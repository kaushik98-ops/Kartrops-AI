# Lambda container image used by the worker, API and email-ingest functions.
# The function handler is chosen per-function by the CDK stack (CMD override).
FROM public.ecr.aws/lambda/python:3.12

COPY pyproject.toml requirements.txt README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

CMD ["kartops.handlers.worker_handler"]
