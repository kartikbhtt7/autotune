import logging

from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.exceptions import ValidationError
from rest_framework.generics import ListAPIView, UpdateAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from .celery_task import process_task
from .dataFetcher import DataFetcher
from .models import Examples, Prompt, Task, WorkflowConfig, Workflows
from .serializers import (
    ExampleSerializer,
    PromptSerializer,
    UserSerializer,
    WorkflowConfigSerializer,
    WorkflowDetailSerializer,
    WorkflowSerializer,
)
from .utils import create_pydantic_model, dehydrate_cache, validate_and_save_examples

logger = logging.getLogger(__name__)


def index():
    return HttpResponse("Hello, world. You're at the workflow index.")


@csrf_exempt
@api_view(["POST"])
def create_workflow_with_prompt(request):
    """
    Creates a new workflow and its associated prompt
    Parameters:
    - request (HttpRequest): The HTTP request object containing the JSON payload.

    Request JSON payload format:
    {
        "workflow": {
            "workflow_name": "Data Analysis Workflow",
            "workflow_config": "QnA",
            "total_examples": 1000,
            "split": [
                70,
                20,
                10
            ],
            "llm_model": "gpt-4-0125-preview",
            "cost": 200,
            "tags": [
                "data analysis",
                "machine learning"
            ],
            "user": "429088bd-73c4-454a-91c7-e29081b36531"
        },
        "user_prompt": "Create questions on world war 2 for class 8 students",
        "examples": [
            {
                "text": "Example question about data analysis?",
                "label": "positive",
                "reason": "Relevant to the domain of data analysis"
            },
            {
                "text": "Example question not related to data analysis.",
                "label": "negative",
                "reason": "Not relevant to the domain"
            }
            // Additional examples can be added here (This is Optional)
        ]
    }

    Returns:
        {
          "workflow": {
            "workflow_id": "123e4567-e89b-12d3-a456-426614174000",
            "workflow_name": "Data Analysis Workflow",
            "total_examples": 1000,
            "split": [70, 20, 10],
            "llm_model": "gpt-4-0125-preview",
            "cost": 200,
            "tags": ["data analysis", "machine learning"],
            "user": "uuid-of-the-user",
            "created_at": "2024-03-07T12:00:00Z",
            "updated_at": "2024-03-07T12:00:00Z"
          },
          "user_prompt": "User provided information to replace {{.DocumentChunk}}",
          "examples": [ // this is Optional
            {
                "example_id": "456f7890-f123-45h6-i789-012j345678k9",
                "text": "Example question about data analysis?",
                "label": "positive",
                "reason": "Relevant to the domain of data analysis",
                "workflow": "123e4567-e89b-12d3-a456-426614174000"
            },
            // Additional examples if provided
          ]
        }
    """

    with transaction.atomic():
        workflow_serializer = WorkflowSerializer(data=request.data.get("workflow", {}))
        if workflow_serializer.is_valid(raise_exception=True):
            workflow = workflow_serializer.save()

            prompt_data = {
                "user_prompt": request.data.get("user_prompt", ""),
                "workflow": workflow.pk,
            }

            prompt_serializer = PromptSerializer(data=prompt_data)
            if prompt_serializer.is_valid(raise_exception=True):
                prompt_serializer.save()

                return Response(
                    {
                        "workflow": workflow_serializer.data,
                        "prompt": prompt_serializer.data,
                    },
                    status=status.HTTP_201_CREATED,
                )

    return Response(
        {
            "error": "Invalid data for workflow or prompt",
        },
        status=status.HTTP_400_BAD_REQUEST,
    )


@api_view(["POST"])
def iterate_workflow(request, workflow_id):
    """
    Iterates over a workflow by either adding new examples or refining existing ones based on the provided data.
    This operation can generate or refine questions and answers based on the examples associated with the workflow.

    Args:
        request (HttpRequest): The request object containing 'examples' data.
        workflow_id (int): The ID of the workflow to be iterated on.

    Sample Request Payload:
        {
            "examples": [
                {
                    "text": "What is AI?",
                    "label": "positive",
                    "reason": "Relevant to the field of study"
                },
                {
                    "text": "What is 2 + 2?",
                    "label": "negative",
                    "reason": "Irrelevant question"
                }
            ]
        }
    Returns:
    - A response object with the outcome of the iteration process. The response structure and data depend on the json schema defined in the configfunction.
    """
    workflow = get_object_or_404(Workflows, pk=workflow_id)
    workflow.status = "ITERATION"
    workflow.save()
    examples_data = request.data.get("examples", [])

    examples_exist = (
        Examples.objects.filter(workflow_id=workflow_id, label__isnull=False).exists()
        or len(examples_data) > 0
    )

    Model, _ = create_pydantic_model(workflow.workflow_config.schema_example)

    success, result = validate_and_save_examples(examples_data, Model, workflow)

    if not success:
        return Response(result, status=status.HTTP_400_BAD_REQUEST)

    user_prompt = request.data.get("user_prompt")
    if user_prompt:
        Prompt.objects.create(user_prompt=user_prompt, workflow=workflow)

    total_examples = request.data.get("total_examples", 10)

    fetcher = DataFetcher()
    fetcher.generate_or_refine(
        workflow_id=workflow.workflow_id,
        total_examples=total_examples,
        workflow_config_id=workflow.workflow_config.id,
        llm_model=workflow.llm_model,
        Model=Model,
        refine=examples_exist,
        iteration=1,
    )
    workflow.status = "IDLE"
    workflow.save()
    return Response(fetcher.examples)


class WorkflowListView(APIView):
    def get(self, request, *args, **kwargs):
        workflows = Workflows.objects.all()
        serializer = WorkflowDetailSerializer(workflows, many=True)
        return Response(serializer.data)

    def post(self, request, *args, **kwargs):
        serializer = WorkflowSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class SingleWorkflowView(APIView):
    def get(self, request, workflow_id, *args, **kwargs):
        workflow = get_object_or_404(Workflows, workflow_id=workflow_id)
        serializer = WorkflowDetailSerializer(workflow)
        return Response(serializer.data)

    def put(self, request, workflow_id, *args, **kwargs):
        workflow = get_object_or_404(Workflows, workflow_id=workflow_id)
        serializer = WorkflowSerializer(workflow, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, workflow_id, *args, **kwargs):
        workflow = get_object_or_404(Workflows, workflow_id=workflow_id)
        workflow.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PromptViewSet(APIView):
    def get(self, request, workflow_id):
        workflow = get_object_or_404(Workflows, pk=workflow_id)
        prompts = (
            workflow.prompts.all()
        )  # Get all prompts associated with this workflow
        return Response(PromptSerializer(prompts, many=True).data)

    def post(self, request, workflow_id):
        workflow = get_object_or_404(Workflows, pk=workflow_id)
        if not request.data.get("user_prompt"):
            return Response(
                {"message": "user_prompt is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        prompt_data = {
            "user_prompt": request.data.get("user_prompt"),
            "workflow": workflow.pk,
        }
        serializer = PromptSerializer(data=prompt_data)
        if serializer.is_valid():
            prompt = serializer.save(workflow=workflow)

            # Update the latest_prompt field on the workflow to this new prompt
            workflow.latest_prompt = prompt
            workflow.save()

            return Response(serializer.data, status=201)
        return Response(serializer.errors, status=400)


class ExamplesView(APIView):

    def get(self, request, workflow_id=None):
        if workflow_id:
            examples = Examples.objects.filter(
                workflow_id=workflow_id, task_id__isnull=True
            )
        else:
            examples = Examples.objects.all()

        serialized_examples = ExampleSerializer(examples, many=True)
        return Response(serialized_examples.data, status=status.HTTP_200_OK)

    def post(self, request, workflow_id):
        workflow = get_object_or_404(Workflows, pk=workflow_id)
        examples_data = request.data.get("examples", [])

        for example_data in examples_data:
            serializer = ExampleSerializer(data=example_data)
            if serializer.is_valid():
                example_id = serializer.validated_data.get("example_id")

                if example_id:
                    try:
                        example = Examples.objects.get(example_id=example_id)
                        example.text = serializer.validated_data["text"]
                        example.label = serializer.validated_data["label"]
                        example.reason = serializer.validated_data["reason"]
                        example.save()
                    except Examples.DoesNotExist:
                        raise ValidationError(
                            f"Example with ID {example_id} does not exist."
                        )
                else:
                    Examples.objects.create(
                        workflow=workflow,
                        text=serializer.validated_data["text"],
                        label=serializer.validated_data["label"],
                        reason=serializer.validated_data["reason"],
                    )

            else:
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        return Response({"message": "Examples updated successfully"}, status=201)


class WorkflowUpdateView(UpdateAPIView):
    """
    Update an existing workflow.

    PUT /workflow/{workflow_id}/update/

    Parameters:
    - workflow_id (URL Path): ID of the workflow to be updated.

    Request Body (application/json):
    {
        "workflow_name": "New Workflow Name",
        "total_examples": 1200,
        ...
    }

    Responses:
    - 200 OK: Workflow successfully updated.
      {
          "workflow_name": "New Workflow Name",
          "total_examples": 1200,
          ...
      }
    - 404 Not Found: If no workflow with the given ID exists.
    """

    queryset = Workflows.objects.all()
    serializer_class = WorkflowSerializer
    lookup_field = "workflow_id"


class WorkflowDuplicateView(APIView):
    """
    Duplicate an existing workflow, creating a new instance with a new ID.

    PUT /workflow/{workflow_id}/duplicate/

    Parameters:
    - workflow_id (URL Path): ID of the workflow to be duplicated.

    Responses:
    - 201 Created: Workflow successfully duplicated.
      {
          "workflow_id": "new-workflow-id",
          ...
      }
    - 404 Not Found: If no workflow with the given ID exists.
    """

    def put(self, request, workflow_id):
        workflow = get_object_or_404(Workflows, workflow_id=workflow_id)
        workflow.pk = None
        workflow.save()
        serializer = WorkflowSerializer(workflow)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class WorkflowStatusView(APIView):
    """
    Retrieve the status of a specific workflow.

    GET /workflow/status/{workflow_id}/

    Parameters:
    - workflow_id (URL Path): ID of the workflow whose status is to be retrieved.

    Responses:
    - 200 OK: Successfully retrieved the status of the workflow.
      {
          "workflow_id": "workflow-id",
          "status": "Workflow Status"
      }
    - 404 Not Found: If no workflow with the given ID exists.
    """

    def get(self, request, workflow_id):
        workflow = get_object_or_404(Workflows, workflow_id=workflow_id)
        return Response({"status": workflow.status})


class WorkflowSearchView(ListAPIView):
    """
    Search for workflows by tag or name.

    GET /workflow/q/?tags=tag1,tag2

    Query Parameters:
    - tags (string): Comma-separated list of tags to filter workflows by.

    Responses:
    - 200 OK: Returns a list of workflows that match the search criteria.
      [
          {
              "workflow_id": "some-workflow-id",
              "workflow_name": "Some Workflow Name",
              ...
          },
          ...
      ]
    """

    serializer_class = WorkflowSerializer

    def get_queryset(self):
        tags_param = self.request.query_params.get("tags", "")
        tags_query = tags_param.split(",") if tags_param else []
        query = Q(tags__overlap=tags_query) if tags_query else Q()
        return Workflows.objects.filter(query)


class TaskView(APIView):

    def get(self, request, task_id):
        task = get_object_or_404(Task, pk=task_id)
        return Response({"status": task.status})


@api_view(["PUT"])
def generate_task(request, workflow_id, *args, **kwargs):
    try:
        workflow = Workflows.objects.get(workflow_id=workflow_id)
    except Workflows.DoesNotExist:
        return JsonResponse({"error": "Workflow not found"}, status=404)
    task = Task.objects.create(
        name=f"Batch Task for Workflow {workflow_id}",
        status="Starting",
        workflow=workflow,
    )

    process_task.delay(task.id)

    return JsonResponse(
        {"message": "Tasks creation initiated", "task_id": task.id}, status=202
    )


@api_view(["GET"])
def dehydrate_cache_view(request, key_pattern):
    """
    A simple view to dehydrate cache entries based on a key pattern.
    """
    dehydrate_cache(key_pattern)
    return JsonResponse(
        {"status": "success", "message": "Cache dehydrated successfully."}
    )


class WorkflowConfigView(APIView):
    """
    Class-based view for managing WorkflowConfig.
    """

    def get(self, request):
        """
        Retrieve all WorkflowConfig objects.
        """
        configs = WorkflowConfig.objects.all()
        serializer = WorkflowConfigSerializer(configs, many=True)
        return Response(serializer.data)

    def post(self, request):
        """
        Create a new WorkflowConfig.
        """
        if request.data.get("schema_example") is None:
            return Response(
                {"message": "Schema Example is required!"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        Model, model_string = create_pydantic_model(request.data.get("schema_example"))
        field_names = list(Model.__fields__.keys())
        field_info = list(Model.__fields__.values())

        fields = []

        for i in range(len(field_names)):
            fields.append({field_names[i]: field_info[i].annotation.__name__})

        data = request.data

        data["model_string"] = model_string
        data["fields"] = fields

        serializer = WorkflowConfigSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(
                {
                    "message": "Workflow config created successfully!",
                    "config": serializer.data,
                },
                status=status.HTTP_201_CREATED,
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def patch(self, request, config_id):
        """
        Update an existing WorkflowConfig based on its ID.
        """
        config = get_object_or_404(WorkflowConfig, id=config_id)
        serializer = WorkflowConfigSerializer(config, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, config_id):
        """
        Delete a WorkflowConfig based on its ID.
        """
        config = get_object_or_404(WorkflowConfig, id=config_id)
        config.delete()
        return Response(
            {"message": "Workflow config deleted successfully!"},
            status=status.HTTP_204_NO_CONTENT,
        )


@api_view(["POST"])
def add_user(request):
    serializer = UserSerializer(data=request.data)

    if serializer.is_valid():
        serializer.save()
        return Response(
            {"message": "User created successfully!", "user": serializer.data},
            status=status.HTTP_201_CREATED,
        )
    else:
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(["POST"])
def train(request):
    # TBD
    return JsonResponse({"message": "hey"})
