#!/usr/bin/env python3
"""Web API server for the Desktop Intelligence Agent."""

from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from pathlib import Path
from datetime import datetime
from threading import Lock

from flask import Flask, request, jsonify, send_from_directory, send_file
from flask_cors import CORS

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import load_config
from memory.sqlite_store import SQLiteStore
from memory.faiss_retriever import FAISSRetriever
from tools import ToolRegistry
from tools.linkup_client import LinkupSearchTool
from tools.email_adapter import ListEmailsTool, ReadEmailTool, DraftReplyTool
from tools.document_adapter import ReadDocumentTool, ListDocumentsTool, SummarizeDocumentTool
from tools.calendar_adapter import ListEventsTool, CreateEventTool, CreateReminderTool
from tools.memory_tools import MemoryStoreTool, MemoryRecallTool
from agent.loop import AgentLoop

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder="frontend", static_url_path="")
CORS(app)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB upload limit

UPLOAD_DIR = Path("data/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

_agent: AgentLoop | None = None
_lock = Lock()


def get_agent() -> AgentLoop:
    global _agent
    if _agent is None:
        cfg = load_config()
        db = SQLiteStore(cfg["memory"]["sqlite_path"])
        faiss = FAISSRetriever(cfg["memory"])
        registry = ToolRegistry()

        registry.register(LinkupSearchTool(cfg.get("linkup", {})))
        email_cfg = cfg.get("email", {})
        registry.register(ListEmailsTool(email_cfg))
        registry.register(ReadEmailTool(email_cfg))
        registry.register(DraftReplyTool())
        doc_cfg = cfg.get("documents", {})
        registry.register(ReadDocumentTool())
        registry.register(ListDocumentsTool(doc_cfg))
        registry.register(SummarizeDocumentTool())
        cal_cfg = cfg.get("calendar", {})
        registry.register(ListEventsTool(cal_cfg))
        registry.register(CreateEventTool(cal_cfg))
        registry.register(CreateReminderTool(cal_cfg))
        registry.register(MemoryStoreTool(db, faiss))
        registry.register(MemoryRecallTool(db, faiss))

        _agent = AgentLoop(cfg, registry, db, faiss)
        logger.info("Agent initialized — session %s", _agent.session_id)
    return _agent


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.route("/")
def index():
    return send_from_directory("frontend", "index.html")


@app.route("/api/chat", methods=["POST"])
def chat():
    """Send a message (with optional file attachments) to the agent."""
    # Support both JSON and multipart/form-data
    if request.content_type and "multipart" in request.content_type:
        message = request.form.get("message", "").strip()
        privacy = request.form.get("privacy", "true").lower() != "false"
        uploaded_files = request.files.getlist("files")

        attachment_paths = []
        for f in uploaded_files:
            if f.filename:
                safe_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{f.filename}"
                save_path = UPLOAD_DIR / safe_name
                f.save(str(save_path))
                attachment_paths.append(str(save_path))
                logger.info("File uploaded: %s", save_path)
    else:
        data = request.get_json(force=True)
        message = data.get("message", "").strip()
        privacy = data.get("privacy", True)
        attachment_paths = []

    if not message:
        return jsonify({"error": "Empty message"}), 400

    agent = get_agent()
    agent.privacy_enabled = privacy

    with _lock:
        try:
            response = agent.run(message, attachments=attachment_paths or None)

            return jsonify({
                "response": response,
                "session_id": agent.session_id,
                "timestamp": datetime.now().isoformat(),
                "privacy_active": privacy,
                "ai_view": agent.last_redacted_input,
                "generated_files": agent.last_generated_files,
                "attachments": [Path(p).name for p in attachment_paths],
            })
        except Exception as exc:
            logger.error("Agent error: %s", traceback.format_exc())
            return jsonify({"error": str(exc)}), 500


@app.route("/api/upload", methods=["POST"])
def upload_file():
    """Upload files for context. Returns saved paths."""
    files = request.files.getlist("files")
    saved = []
    for f in files:
        if f.filename:
            safe_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{f.filename}"
            save_path = UPLOAD_DIR / safe_name
            f.save(str(save_path))
            saved.append({"name": f.filename, "path": str(save_path), "size_kb": round(save_path.stat().st_size / 1024, 1)})
    return jsonify({"files": saved})


@app.route("/api/download")
def download_file():
    """Download a generated file (.ics, .eml, etc.)."""
    filepath = request.args.get("path", "")
    p = Path(filepath)
    if not p.exists():
        return jsonify({"error": "File not found"}), 404
    # Security: only allow files from data/ directory
    try:
        p.resolve().relative_to(Path("data").resolve())
    except ValueError:
        return jsonify({"error": "Access denied"}), 403
    return send_file(str(p.resolve()), as_attachment=True, download_name=p.name)


@app.route("/api/download/ics")
def download_ics():
    """Legacy endpoint — redirects to generic download."""
    return download_file()


@app.route("/api/download/eml")
def download_eml():
    """Legacy endpoint — redirects to generic download."""
    return download_file()


@app.route("/api/privacy", methods=["POST"])
def toggle_privacy():
    """Toggle privacy mode on/off."""
    data = request.get_json(force=True)
    enabled = data.get("enabled", True)
    agent = get_agent()
    agent.privacy_enabled = bool(enabled)
    return jsonify({"privacy_enabled": agent.privacy_enabled})


@app.route("/api/privacy", methods=["GET"])
def get_privacy():
    """Get current privacy status."""
    agent = get_agent()
    # Check which engine is available
    from tools.privacy import _load_presidio
    _, _, nlp_ok = _load_presidio()
    return jsonify({
        "privacy_enabled": agent.privacy_enabled,
        "engine": "presidio" if nlp_ok else "regex",
    })


@app.route("/api/tools", methods=["GET"])
def list_tools():
    agent = get_agent()
    tools = []
    for t in agent._registry.all_tools():
        tools.append({
            "name": t.name,
            "description": t.description,
            "parameters": t.parameters,
        })
    return jsonify({"tools": tools})


@app.route("/api/tasks", methods=["GET"])
def list_tasks():
    agent = get_agent()
    tasks = agent._db.recent_tasks(agent.session_id, limit=20)
    return jsonify({"tasks": tasks})


@app.route("/api/memory", methods=["GET"])
def search_memory():
    query = request.args.get("q", "recent")
    agent = get_agent()
    results = agent._faiss.search(query, top_k=10)
    return jsonify({"memories": results})


@app.route("/api/session", methods=["GET"])
def session_info():
    agent = get_agent()
    return jsonify({
        "session_id": agent.session_id,
        "tools_count": len(agent._registry.names()),
        "tools": agent._registry.names(),
        "memory_size": agent._faiss.size,
        "privacy_enabled": agent.privacy_enabled,
    })


if __name__ == "__main__":
    Path("frontend").mkdir(exist_ok=True)
    print("\n🤖 Desktop Agent API running at http://localhost:5000\n")
    app.run(host="0.0.0.0", port=5000, debug=False)
