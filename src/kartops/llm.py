"""LLM access via the Bedrock Converse API with forced tool use.
Every node ends by calling `submit_result` with its JSON schema, so no free-text JSON parsing is needed."""
from __future__ import annotations

import json
import logging
from typing import Any, Protocol

import jsonschema

from .config import Settings
from .tools import ToolBox

log = logging.getLogger("kartops.llm")


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    def run(self, *, node: str, base: str, node_prompt: str, user_text: str, payload: dict,
            tools: ToolBox | None, schema: dict) -> tuple[dict, dict]: ...


class BedrockLLM:
    FAST_NODES = {"router", "verifier"}

    def __init__(self, s: Settings):
        import boto3
        from botocore.config import Config
        self.s = s
        self.cache = s.prompt_cache
        self.client = boto3.client(
            "bedrock-runtime", region_name=s.region,
            config=Config(retries={"max_attempts": 6, "mode": "adaptive"}, read_timeout=120, connect_timeout=10))
        if not (s.model_fast and s.model_strong):
            raise LLMError("MODEL_FAST and MODEL_STRONG must be set in aws mode")

    def _system(self, base: str, node_prompt: str) -> list[dict]:
        blocks: list[dict] = [{"text": base}]
        if self.cache:
            blocks.append({"cachePoint": {"type": "default"}})  # BASE is identical across nodes
        blocks.append({"text": node_prompt})
        return blocks

    def run(self, *, node, base, node_prompt, user_text, payload, tools, schema):
        from botocore.exceptions import ClientError
        model = self.s.model_fast if node in self.FAST_NODES else self.s.model_strong
        submit = {"toolSpec": {"name": "submit_result",
                               "description": "Submit your final result. Call exactly once, with no other text.",
                               "inputSchema": {"json": schema}}}
        tool_specs = (tools.specs() if tools else []) + [submit]
        messages: list[dict] = [{"role": "user", "content": [{"text": user_text}]}]
        usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "model": model}
        repairs = 0

        for _turn in range(self.s.max_tool_turns):
            kwargs: dict[str, Any] = dict(
                modelId=model, system=self._system(base, node_prompt), messages=messages,
                inferenceConfig={"maxTokens": 2048 if node in self.FAST_NODES else 4096, "temperature": 0.0},
                toolConfig={"tools": tool_specs, "toolChoice": {"any": {}}},
            )
            try:
                resp = self.client.converse(**kwargs)
            except ClientError as e:
                if self.cache and "cache" in str(e).lower():  # model without prompt caching support
                    log.warning("prompt caching rejected by model; disabling")
                    self.cache = False
                    kwargs["system"] = self._system(base, node_prompt)
                    resp = self.client.converse(**kwargs)
                else:
                    raise
            u = resp.get("usage", {})
            usage["input"] += u.get("inputTokens", 0)
            usage["output"] += u.get("outputTokens", 0)
            usage["cache_read"] += u.get("cacheReadInputTokens", 0)
            usage["cache_write"] += u.get("cacheWriteInputTokens", 0)

            msg = resp["output"]["message"]
            messages.append(msg)
            uses = [b["toolUse"] for b in msg["content"] if "toolUse" in b]
            if not uses:
                messages.append({"role": "user", "content": [{"text": "Call submit_result now."}]})
                continue

            results = []
            final = None
            for tu in uses:
                if tu["name"] == "submit_result":
                    try:
                        jsonschema.validate(tu["input"], schema)
                        final = tu["input"]
                        results.append({"toolResult": {"toolUseId": tu["toolUseId"], "content": [{"json": {"ok": True}}]}})
                    except jsonschema.ValidationError as ve:
                        repairs += 1
                        if repairs > 2:
                            raise LLMError(f"{node}: schema validation failed repeatedly: {ve.message}") from ve
                        results.append({"toolResult": {"toolUseId": tu["toolUseId"], "status": "error",
                                                       "content": [{"text": f"Schema error: {ve.message}. Resubmit."}]}})
                else:
                    out = tools.call(tu["name"], tu["input"]) if tools else {"error": "no tools"}
                    results.append({"toolResult": {"toolUseId": tu["toolUseId"],
                                                   "content": [{"text": json.dumps(out, default=str)[:20000]}]}})
            if final is not None:
                return final, usage
            messages.append({"role": "user", "content": results})
        raise LLMError(f"{node}: no valid submit_result within {self.s.max_tool_turns} turns")


def get_llm(s: Settings) -> LLM:
    if s.is_aws or s.model_fast:
        return BedrockLLM(s)
    from .fake_llm import FakeLLM
    return FakeLLM()
