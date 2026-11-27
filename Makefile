.PHONY: install test demo eval synth deploy destroy seed zip
MF ?= $(MODEL_FAST)
MS ?= $(MODEL_STRONG)
CTX = -c model_fast=$(MF) -c model_strong=$(MS) -c kb_id=$(KB_ID) -c alert_email=$(ALERT_EMAIL)

install:
	pip install -e ".[dev]"

test:
	pytest -q

demo:            ## runs offline with the rule-based FakeLLM (no AWS needed)
	kartops demo

eval:
	kartops eval

synth:
	cd infra && pip install -q -r requirements.txt && npx aws-cdk@2 synth $(CTX)

deploy:          ## needs Docker running + AWS credentials + MODEL_FAST / MODEL_STRONG exported
	cd infra && pip install -q -r requirements.txt && npx aws-cdk@2 deploy $(CTX) --require-approval broadening

destroy:
	cd infra && npx aws-cdk@2 destroy $(CTX)

seed:            ## load demo data into DATA_TABLE (KARTOPS_MODE=aws DATA_TABLE=... AWS_REGION=...)
	kartops seed
