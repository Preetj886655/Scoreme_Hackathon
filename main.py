import json
import uuid
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from models import init_db, get_db, WorkflowRequest, AuditLog, StateHistory
from engine import run_workflow
from config_loader import load_all_configs, load_config, validate_config
import yaml, os, tempfile

# ── Load all workflows on startup ──
WORKFLOW_CONFIGS: dict = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    global WORKFLOW_CONFIGS
    WORKFLOW_CONFIGS = load_all_configs("workflows")
    print(f"🚀 Loaded {len(WORKFLOW_CONFIGS)} workflow(s): {list(WORKFLOW_CONFIGS.keys())}")
    yield

app = FastAPI(
    title="Workflow Decision Engine",
    description="A configurable workflow decision platform",
    version="1.0.0",
    lifespan=lifespan
)

app.mount("/static", StaticFiles(directory="static"), name="static")


# ─────────────────────────────────────────────
# SCHEMAS
# ─────────────────────────────────────────────
class SubmitRequest(BaseModel):
    workflow_name: str
    idempotency_key: str | None = None
    input_data: dict


class RetryRequest(BaseModel):
    request_id: str


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def root():
    with open("static/index.html") as f:
        return f.read()


@app.get("/health")
def health():
    return {
        "status": "ok",
        "loaded_workflows": list(WORKFLOW_CONFIGS.keys())
    }


@app.get("/workflows")
def list_workflows():
    """List all available workflows and their rules."""
    result = {}
    for name, config in WORKFLOW_CONFIGS.items():
        result[name] = {
            "description": config.get("description", ""),
            "steps": config.get("steps", []),
            "rules": [
                {
                    "name": r["name"],
                    "field": r["field"],
                    "operator": r["operator"],
                    "value": r["value"],
                    "on_fail": r["on_fail"],
                    "message": r["message"]
                }
                for r in config.get("rules", [])
            ]
        }
    return result


@app.post("/submit")
def submit_request(payload: SubmitRequest, db: Session = Depends(get_db)):
    """Submit a new workflow request."""

    # Validate workflow exists
    if payload.workflow_name not in WORKFLOW_CONFIGS:
        raise HTTPException(
            status_code=404,
            detail=f"Workflow '{payload.workflow_name}' not found. Available: {list(WORKFLOW_CONFIGS.keys())}"
        )

    # Generate idempotency key if not provided
    idem_key = payload.idempotency_key or str(uuid.uuid4())

    # ── Idempotency check ──
    existing = db.query(WorkflowRequest).filter_by(idempotency_key=idem_key).first()
    if existing:
        return {
            "message": "Duplicate request detected. Returning existing result.",
            "request_id": existing.id,
            "status": existing.status,
            "idempotency_key": idem_key,
            "duplicate": True
        }

    # Create DB record
    req = WorkflowRequest(
        idempotency_key=idem_key,
        workflow_name=payload.workflow_name,
        input_data=json.dumps(payload.input_data),
        status="pending"
    )
    db.add(req)
    db.commit()
    db.refresh(req)

    # Run workflow engine
    config = WORKFLOW_CONFIGS[payload.workflow_name]
    result = run_workflow(req, config, payload.input_data, db)

    return {
        "request_id": req.id,
        "idempotency_key": idem_key,
        "workflow": payload.workflow_name,
        "status": result["status"],
        "decision": result["decision"],
        "reason": result["reason"],
        "audit_trail": result["audit_trail"],
        "duplicate": False
    }


@app.post("/retry/{request_id}")
def retry_request(request_id: str, db: Session = Depends(get_db)):
    """Retry a failed or retryable request."""
    req = db.query(WorkflowRequest).filter_by(id=request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")

    if req.status not in ["retry", "failed"]:
        raise HTTPException(
            status_code=400,
            detail=f"Request status is '{req.status}'. Only 'retry' or 'failed' requests can be retried."
        )

    if req.retry_count >= 3:
        raise HTTPException(status_code=400, detail="Max retry limit (3) reached. Escalating to manual review.")

    req.retry_count += 1
    db.commit()

    config = WORKFLOW_CONFIGS.get(req.workflow_name)
    if not config:
        raise HTTPException(status_code=404, detail=f"Workflow config '{req.workflow_name}' no longer exists")

    input_data = json.loads(req.input_data)
    result = run_workflow(req, config, input_data, db)

    return {
        "request_id": req.id,
        "retry_count": req.retry_count,
        "status": result["status"],
        "decision": result["decision"],
        "reason": result["reason"],
        "audit_trail": result["audit_trail"]
    }


@app.get("/status/{request_id}")
def get_status(request_id: str, db: Session = Depends(get_db)):
    """Get current status of a request."""
    req = db.query(WorkflowRequest).filter_by(id=request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")

    return {
        "request_id": req.id,
        "workflow": req.workflow_name,
        "status": req.status,
        "retry_count": req.retry_count,
        "created_at": req.created_at,
        "updated_at": req.updated_at
    }


@app.get("/audit/{request_id}")
def get_audit(request_id: str, db: Session = Depends(get_db)):
    """Get full audit trail for a request."""
    req = db.query(WorkflowRequest).filter_by(id=request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")

    logs = db.query(AuditLog).filter_by(request_id=request_id).order_by(AuditLog.timestamp).all()
    history = db.query(StateHistory).filter_by(request_id=request_id).order_by(StateHistory.timestamp).all()

    return {
        "request_id": request_id,
        "workflow": req.workflow_name,
        "current_status": req.status,
        "input_data": json.loads(req.input_data),
        "rule_evaluations": [
            {
                "step": l.step,
                "rule": l.rule_name,
                "field": l.field,
                "expected": l.expected,
                "actual": l.actual_value,
                "result": l.result,
                "reason": l.reason,
                "timestamp": l.timestamp
            } for l in logs
        ],
        "state_history": [
            {
                "from": h.old_status,
                "to": h.new_status,
                "reason": h.reason,
                "timestamp": h.timestamp
            } for h in history
        ]
    }


@app.get("/requests")
def list_requests(db: Session = Depends(get_db)):
    """List all requests."""
    requests = db.query(WorkflowRequest).order_by(WorkflowRequest.created_at.desc()).limit(50).all()
    return [
        {
            "request_id": r.id,
            "workflow": r.workflow_name,
            "status": r.status,
            "retry_count": r.retry_count,
            "created_at": r.created_at
        }
        for r in requests
    ]


@app.post("/upload-config")
async def upload_config(file: UploadFile = File(...)):
    """
    Upload a custom config file (YAML or JSON).
    If it doesn't match our format, Gemini will attempt to convert it.
    """
    content = await file.read()
    suffix = ".yaml" if file.filename.endswith((".yaml", ".yml")) else ".json"

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        config = load_config(tmp_path)
        workflow_name = config["workflow_name"]
        WORKFLOW_CONFIGS[workflow_name] = config

        # Save to workflows folder
        out_path = f"workflows/{workflow_name}.yaml"
        with open(out_path, "w") as f:
            yaml.dump(config, f)

        return {
            "message": f"✅ Config uploaded and loaded as workflow '{workflow_name}'",
            "workflow_name": workflow_name,
            "steps": config["steps"],
            "rules_count": len(config["rules"])
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        os.unlink(tmp_path)
