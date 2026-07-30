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
            maximum_attempts=2,
            backoff_coefficient=2,
        )

        workflow_args: Dict[str, Any] = await workflow.execute_activity_method(
            activities_instance.get_workflow_args,
            workflow_config,
            retry_policy=args_retry_policy,
            start_to_close_timeout=timedelta(seconds=10),
        )
        extraction_result = await workflow.execute_activity_method(
            activities_instance.extract_and_transform_metadata,
            workflow_args,
            retry_policy=extract_retry_policy,
            start_to_close_timeout=timedelta(hours=2),
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
