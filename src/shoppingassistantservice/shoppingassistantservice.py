"""Shopping assistant service (Amazon Bedrock tool-calling agent).

Contract with the frontend is unchanged: POST / {message, ...} -> {content, ...}.
Optional request fields: session_id, prompt_category, conversation_id. The same
values are also accepted as X-Session-ID / X-Prompt-Category / X-Conversation-ID
headers. The `image` field the old Gemini UI sent is ignored.
"""
import os

from flask import Flask, jsonify, request

import agent
import config
import telemetry as tel

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # the legacy UI may still POST a base64 image
exporting = tel.init(app)


def _field(body, key, header, default=""):
    return str(body.get(key) or request.headers.get(header) or default).strip()


@app.get("/healthz")
def healthz():
    return "ok", 200


@app.post("/")
def chat():
    body = request.get_json(silent=True) or {}
    message = str(body.get("message") or "").strip()
    if not message:
        return jsonify({"content": "Please tell me what you're looking for.", "error": "empty message"}), 400

    session_id = _field(body, "session_id", "X-Session-ID", "anonymous")[:64]
    category = _field(body, "prompt_category", "X-Prompt-Category", "uncategorized")[:48]
    conversation = _field(body, "conversation_id", "X-Conversation-ID", session_id)[:64]

    # Make the customer identity travel on every outgoing gRPC call, like the
    # frontend does for app.session.id.
    token = tel.attach_baggage(**{"app.session.id": session_id, "app.customer.id": session_id,
                                  "app.prompt.category": category})
    try:
        session = agent.get_session(session_id)
        with session.lock:  # one in-flight prompt per conversation
            result = agent.run(session, message, category, conversation)
    finally:
        tel.detach_baggage(token)

    status = 200
    outcome = result["details"]["outcome"]
    if outcome == "rate_limited":
        status = 429
    elif outcome == "not_configured":
        status = 503
    return jsonify(result), status


if __name__ == "__main__":
    tel.event("assistant.starting", **{
        "llm.provider": "aws.bedrock", "bedrock.model_id": config.BEDROCK_MODEL_ID,
        "aws.region": config.AWS_REGION,
        "prompt.version": config.PROMPT_VERSION, "telemetry.exporting": exporting,
        "fault.injection": sorted(config.FAULT_INJECTION)})
    app.run(host="0.0.0.0", port=config.PORT, threaded=True)
