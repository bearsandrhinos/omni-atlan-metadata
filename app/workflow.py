from datetime import timedelta
from typing import Any, Callable, Dict, Sequence

from app.activities import ActivitiesClass
from application_sdk.activities import ActivitiesInterface
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.workflows import WorkflowInterface
from temporalio import workflow
from temporalio.common import RetryPolicy

logger = get_logger(__name__)
workflow.logger = logger

# Fleet-standard activity timeouts (mirrors the Cyera app's constants.py:
# FETCH_START_TO_CLOSE_TIMEOUT / FETCH_HEARTBEAT_TIMEOUT / SHORT_ACTIVITY_TIMEOUT).
# The heartbeat is what makes the ceiling enforceable — Temporal delivers the
# timeout/cancellation to the worker on a heartbeat, so without one a timed-out
# activity keeps running unobserved (one run continued ~9h44m past its deadline).
# Restored to the original 8h (v0.2.7). Atlan asked for 2h in July on the
# grounds that 8h "hides hung runs" — the heartbeat below now covers that case
# in ~5 minutes, so the ceiling's only remaining job is to bound total work.
# Measured post-fix runtime on the largest deployment we have data for is ~3h,
# so 8h leaves ~2.7x headroom for growth. Workflow execution_timeout is 24h
# (measured), so this fits.
FETCH_START_TO_CLOSE_TIMEOUT = timedelta(hours=8)
FETCH_HEARTBEAT_TIMEOUT = timedelta(minutes=5)
SHORT_ACTIVITY_TIMEOUT = timedelta(seconds=60)


@workflow.defn(name="OmniMetadataExtractionWorkflow")
class WorkflowClass(WorkflowInterface):
    @workflow.run
    async def run(self, workflow_config: Dict[str, Any]) -> None:
        activities_instance = ActivitiesClass()

        args_retry_policy = RetryPolicy(
            maximum_attempts=2,
            backoff_coefficient=2,
        )
        extract_retry_policy = RetryPolicy(
            # One attempt, not two: a retry restarts the crawl from zero, so on a
            # multi-hour extraction its value is near nil while its cost is a
            # second full window of load on the source API. Raise this again once
            # the fetch checkpoints and can resume.
            maximum_attempts=1,
            backoff_coefficient=2,
        )

        workflow_args: Dict[str, Any] = await workflow.execute_activity_method(
            activities_instance.get_workflow_args,
            workflow_config,
            retry_policy=args_retry_policy,
            start_to_close_timeout=SHORT_ACTIVITY_TIMEOUT,
        )
        extraction_result = await workflow.execute_activity_method(
            activities_instance.extract_and_transform_metadata,
            workflow_args,
            retry_policy=extract_retry_policy,
            start_to_close_timeout=FETCH_START_TO_CLOSE_TIMEOUT,
            heartbeat_timeout=FETCH_HEARTBEAT_TIMEOUT,
        )
        workflow.logger.info("Omni extraction completed: %s", extraction_result)

    @staticmethod
    def get_activities(activities: ActivitiesInterface) -> Sequence[Callable[..., Any]]:
        if not isinstance(activities, ActivitiesClass):
            raise TypeError("Activities must be an instance of ActivitiesClass")

        return [
            activities.get_workflow_args,
            activities.extract_and_transform_metadata,
        ]
