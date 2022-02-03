#
# Copyright (c) 2021 Airbyte, Inc., all rights reserved.
#

import copy
import logging
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, List, Mapping, Optional, Sequence, Iterator, Union

import pendulum
from facebook_business.adobjects.adaccount import AdAccount
from facebook_business.adobjects.adreportrun import AdReportRun
from facebook_business.adobjects.campaign import Campaign
from facebook_business.adobjects.objectparser import ObjectParser
from facebook_business.api import FacebookAdsApiBatch, FacebookResponse, FacebookAdsApi

logger = logging.getLogger("airbyte")


def chunks(data: Sequence[Any], n: int) -> Iterator[Any]:
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(data), n):
        yield data[i : i + n]


class Status(str, Enum):
    """Async job statuses"""

    COMPLETED = "Job Completed"
    FAILED = "Job Failed"
    SKIPPED = "Job Skipped"
    STARTED = "Job Started"
    RUNNING = "Job Running"
    NOT_STARTED = "Job Not Started"


class AsyncJob(ABC):
    """Abstract AsyncJob base class"""

    def __init__(self, api: FacebookAdsApi, interval: pendulum.Period):
        """ Init generic async job

        :param api: FB API instance (to create batch, etc)
        :param interval: interval for which the job will fetch data
        """
        self._api = api
        self._interval = interval
        self._attempt_number = 1

    @property
    def key(self) -> str:
        """Job identifier, in most cases start of the interval"""
        return str(self._interval.start.date())

    @abstractmethod
    def start(self):
        """Start remote job"""

    @abstractmethod
    def restart(self):
        """Restart failed job"""

    @property
    def attempt_number(self):
        """Number of attempts"""
        return self._attempt_number

    @property
    @abstractmethod
    def completed(self) -> bool:
        """Check job status and return True if it is completed, use failed/succeeded to check if it was successful"""

    @property
    @abstractmethod
    def failed(self) -> bool:
        """Tell if the job previously failed"""

    @abstractmethod
    def update_job(self, batch: Optional[FacebookAdsApiBatch] = None):
        """ Method to retrieve job's status, separated because of retry handler

        :param batch: FB batch executor
        """

    @abstractmethod
    def get_result(self) -> Iterator[Any]:
        """Retrieve result of the finished job."""

    @abstractmethod
    def split_job(self) -> "AsyncJob":
        """Split existing job in few smaller ones grouped by ParentAsyncJob"""


class ParentAsyncJob(AsyncJob):
    """Group of async jobs"""

    def __init__(self, jobs: List[AsyncJob], **kwargs):
        """Initialize jobs"""
        super().__init__(**kwargs)
        self._jobs = jobs

    def start(self):
        """Start each job in the group."""
        for job in self._jobs:
            job.start()

    def restart(self):
        """Restart failed jobs"""
        for job in self._jobs:
            if job.failed:
                job.restart()
            self._attempt_number = max(self._attempt_number, job.attempt_number)

    @property
    def completed(self) -> bool:
        """Check job status and return True if all jobs are completed, use failed/succeeded to check if it was successful"""
        return all(job.completed for job in self._jobs)

    @property
    def failed(self) -> bool:
        """Tell if any job previously failed"""
        return any(job.failed for job in self._jobs)

    def update_job(self, batch: Optional[FacebookAdsApiBatch] = None):
        """Checks jobs status in advance and restart if some failed."""
        batch = self._api.new_batch()
        unfinished_jobs = [job for job in self._jobs if not job.completed]
        for jobs in chunks(unfinished_jobs, 50):
            for job in jobs:
                job.update_job(batch=batch)

            while batch:
                # If some of the calls from batch have failed, it returns  a new
                # FacebookAdsApiBatch object with those calls
                batch = batch.execute()

    def get_result(self) -> Iterator[Any]:
        """Retrieve result of the finished job."""
        for job in self._jobs:
            yield from job.get_result()

    def split_job(self) -> "AsyncJob":
        """Split existing job in few smaller ones grouped by ParentAsyncJob class. Will be implemented in future versions."""
        raise RuntimeError("Splitting of ParentAsyncJob is not allowed.")


class InsightAsyncJob(AsyncJob):
    """AsyncJob wraps FB AdReport class and provides interface to restart/retry the async job"""

    page_size = 100

    def __init__(self, edge_object: Union[AdAccount, Campaign], params: Mapping[str, Any], **kwargs):
        """Initialize

        :param api: FB API
        :param edge_object: Account, Campaign, (AdSet or Ad in future)
        :param params: job params, required to start/restart job
        """
        super().__init__(**kwargs)
        self._params = dict(params)
        self._params["time_range"] = {
            "since": self._interval.start.to_date_string(),
            "until": self._interval.end.to_date_string(),
        }

        self._edge_object = edge_object
        self._job: Optional[AdReportRun] = None
        self._start_time = None
        self._finish_time = None
        self._failed = False

    def split_job(self) -> "AsyncJob":
        """Split existing job in few smaller ones grouped by ParentAsyncJob class.
        TODO: use some cache to avoid expensive queries across different streams.
        """
        campaign_params = dict(copy.deepcopy(self._params))
        # get campaigns from attribution window as well (28 day + 1 current day)
        new_start = self._interval.start.date() - pendulum.duration(days=28 + 1)
        campaign_params.update(fields=["campaign_id"], level="campaign")
        campaign_params["time_range"].update(since=new_start.to_date_string())
        campaign_params.pop("time_increment")  # query all days
        result = self._edge_object.get_insights(params=campaign_params)
        campaign_ids = set(row["campaign_id"] for row in result)
        logger.info(
            "Got %(num)s campaigns for period %(period)s: %(campaign_ids)s",
            num=len(campaign_ids),
            period=self._params['time_range'],
            campaign_ids=campaign_ids
        )

        jobs = [InsightAsyncJob(api=self._api, edge_object=Campaign(pk), params=self._params) for pk in campaign_ids]
        return ParentAsyncJob(api=self._api, interval=self._interval, jobs=jobs)

    def start(self):
        """Start remote job"""
        if self._job:
            raise RuntimeError(f"{self}: Incorrect usage of start - the job already started, use restart instead")

        self._job = self._edge_object.get_insights(params=self._params, is_async=True)
        self._start_time = pendulum.now()
        logger.info(
            "Created AdReportRun: %(job_id)s to sync insights %(time_range)s with breakdown %(breakdowns)s for %(obj)s",
            job_id = self._job["report_run_id"],
            time_range=self._params["time_range"],
            breakdowns=self._params["breakdowns"],
            obj=self._edge_object,
        )

    def restart(self):
        """Restart failed job"""
        if not self._job or not self.failed:
            raise RuntimeError(f"{self}: Incorrect usage of restart - only failed jobs can be restarted")

        self._job = None
        self._failed = False
        self._start_time = None
        self._finish_time = None
        self._attempt_number += 1
        self.start()
        logger.info("%s: restarted", self)

    @property
    def elapsed_time(self) -> Optional[pendulum.duration]:
        """Elapsed time since the job start"""
        if not self._start_time:
            return None

        end_time = self._finish_time or pendulum.now()
        return end_time - self._start_time

    @property
    def completed(self) -> bool:
        """Check job status and return True if it is completed, use failed/succeeded to check if it was successful

        :return: True if completed, False - if task still running
        :raises: JobException in case job failed to start, failed or timed out
        """
        return bool(self._finish_time is not None)

    @property
    def failed(self) -> bool:
        """Tell if the job previously failed"""
        return self._failed

    def _batch_success_handler(self, response: FacebookResponse):
        """Update job status from response"""
        self._job = ObjectParser(reuse_object=self._job).parse_single(response.json())
        self._check_status()

    def _batch_failure_handler(self, response: FacebookResponse):
        """Update job status from response"""
        logger.info("Request failed with response: %s", response.body())

    def update_job(self, batch: Optional[FacebookAdsApiBatch] = None):
        """Method to retrieve job's status, separated because of retry handler"""
        if not self._job:
            raise RuntimeError(f"{self}: Incorrect usage of the method - the job is not started")

        if self.completed:
            logger.info(
                "%(job)s is %(percent)s complete (%(status)s)",
                job=self, percent=self._job["async_percent_completion"], status=self._job['async_status']
            )
            # No need to update job status if its already completed
            return

        if batch is not None:
            self._job.api_get(batch=batch, success=self._batch_success_handler, failure=self._batch_failure_handler)
        else:
            self._job = self._job.api_get()
            self._check_status()

    def _check_status(self) -> bool:
        """Perform status check

        :return: True if the job is completed, False - if the job is still running
        """
        job_status = self._job["async_status"]
        logger.info(
            "%(job)s is %(percent)s complete (%(status)s)",
            job=self, percent=self._job["async_percent_completion"], status=job_status,
        )

        if job_status == Status.COMPLETED:
            self._finish_time = pendulum.now()  # TODO: is not actual running time, but interval between check_status calls
            return True
        elif job_status in [Status.FAILED, Status.SKIPPED]:
            self._finish_time = pendulum.now()
            self._failed = True
            logger.info(
                "%(job)s has status %(status)s after %(elapsed)s seconds.",
                job=self, status=job_status, elapsed=self.elapsed_time.in_seconds(),
            )
            return True

        return False

    def get_result(self) -> Any:
        """Retrieve result of the finished job."""
        if not self._job or self.failed:
            raise RuntimeError(f"{self}: Incorrect usage of get_result - the job is not started or failed")
        return self._job.get_result(params={"limit": self.page_size})

    def __str__(self) -> str:
        """String representation of the job wrapper."""
        job_id = self._job["report_run_id"] if self._job else "<None>"
        time_range = self._params["time_range"]
        breakdowns = self._params["breakdowns"]
        return f"InsightAsyncJob(id={job_id}, {self._edge_object}, time_range={time_range}, breakdowns={breakdowns}"
