import json
import random
from datetime import datetime
from sqlalchemy.orm import Session
from models import WorkflowRequest, AuditLog, StateHistory


# ─────────────────────────────────────────────
# RULE EVALUATOR
# ─────────────────────────────────────────────
def evaluate_rule(rule: dict, input_data: dict) -> tuple[bool, str]:
    """
    Evaluate a single rule against input data.
    Returns (passed: bool, reason: str)
    """
    field = rule["field"]
    operator = rule["operator"]
    expected = rule["value"]
    actual = input_data.get(field)

    if actual is None:
        return False, f"Field '{field}' is missing from input data"

    try:
        if operator == ">=":
            passed = float(actual) >= float(expected)
        elif operator == "<=":
            passed = float(actual) <= float(expected)
        elif operator == ">":
            passed = float(actual) > float(expected)
        elif operator == "<":
            passed = float(actual) < float(expected)
        elif operator == "==":
            passed = str(actual) == str(expected)
        elif operator == "!=":
            passed = str(actual) != str(expected)
        elif operator == "in":
            passed = actual in expected
        elif operator == "not_in":
            passed = actual not in expected
        else:
            return False, f"Unknown operator: {operator}"
    except (TypeError, ValueError) as e:
        return False, f"Type error evaluating rule '{rule['name']}': {e}"

    reason = rule["message"] if not passed else f"'{field}' {operator} {expected} ✓ (actual: {actual})"
    return passed, reason


# ─────────────────────────────────────────────
# EXTERNAL DEPENDENCY SIMULATOR
# ─────────────────────────────────────────────
def call_external_dependency(dep_config: dict) -> tuple[bool, str]:
    """
    Simulates an external API call.
    Has a 20% random failure rate to demonstrate retry logic.
    """
    name = dep_config.get("name", "external_service")
    simulate = dep_config.get("simulate", True)

    if not simulate:
        # In real life, make actual HTTP call here
        return True, f"{name} responded successfully"

    # Simulate 20% failure
    if random.random() < 0.20:
        return False, f"⚠️ External dependency '{name}' is unavailable (simulated failure)"

    return True, f"✅ External dependency '{name}' responded successfully"


# ─────────────────────────────────────────────
# STATE TRACKER
# ─────────────────────────────────────────────
def update_state(db: Session, request: WorkflowRequest, new_status: str, reason: str = ""):
    old_status = request.status
    request.status = new_status
    request.updated_at = datetime.utcnow()

    history = StateHistory(
        request_id=request.id,
        old_status=old_status,
        new_status=new_status,
        reason=reason
    )
    db.add(history)
    db.commit()


def write_audit(db: Session, request_id: str, workflow_name: str,
                step: str, rule_name: str, field: str,
                expected: str, actual_value: str, result: str, reason: str):
    log = AuditLog(
        request_id=request_id,
        workflow_name=workflow_name,
        step=step,
        rule_name=rule_name,
        field=field,
        expected=str(expected),
        actual_value=str(actual_value),
        result=result,
        reason=reason
    )
    db.add(log)
    db.commit()


# ─────────────────────────────────────────────
# MAIN ENGINE
# ─────────────────────────────────────────────
def run_workflow(
    request: WorkflowRequest,
    workflow_config: dict,
    input_data: dict,
    db: Session
) -> dict:
    """
    Execute a workflow against input data.
    Returns a result dict with status, decision, and audit trail.
    """
    workflow_name = workflow_config["workflow_name"]
    rules = workflow_config["rules"]
    dep_config = workflow_config.get("external_dependency")

    audit_trail = []
    final_status = "approved"
    rejection_reason = ""

    # ── Step 1: External Dependency Check ──
    if dep_config:
        update_state(db, request, "checking_dependency", "Calling external dependency")
        dep_ok, dep_msg = call_external_dependency(dep_config)

        write_audit(
            db, request.id, workflow_name,
            step="external_dependency",
            rule_name=dep_config.get("name", "external_service"),
            field="external_call",
            expected="success",
            actual_value="success" if dep_ok else "failure",
            result="passed" if dep_ok else "failed",
            reason=dep_msg
        )
        audit_trail.append({"step": "external_dependency", "result": "passed" if dep_ok else "failed", "reason": dep_msg})

        if not dep_ok:
            failure_action = dep_config.get("failure_action", "retry")
            update_state(db, request, failure_action, dep_msg)
            return {
                "status": failure_action,
                "decision": failure_action,
                "reason": dep_msg,
                "audit_trail": audit_trail
            }

    # ── Step 2: Evaluate Rules ──
    update_state(db, request, "evaluating_rules", "Running rule evaluation")

    for rule in rules:
        passed, reason = evaluate_rule(rule, input_data)
        result_label = "passed" if passed else "failed"

        write_audit(
            db, request.id, workflow_name,
            step="rule_evaluation",
            rule_name=rule["name"],
            field=rule["field"],
            expected=rule["value"],
            actual_value=input_data.get(rule["field"], "N/A"),
            result=result_label,
            reason=reason
        )
        audit_trail.append({
            "step": "rule_evaluation",
            "rule": rule["name"],
            "field": rule["field"],
            "result": result_label,
            "reason": reason
        })

        if not passed:
            on_fail = rule.get("on_fail", "reject")
            final_status = on_fail
            rejection_reason = reason

            # Stop at first failure (fail-fast)
            update_state(db, request, on_fail, reason)
            return {
                "status": on_fail,
                "decision": on_fail,
                "reason": rejection_reason,
                "failed_rule": rule["name"],
                "audit_trail": audit_trail
            }

    # ── Step 3: All Rules Passed ──
    update_state(db, request, "approved", "All rules passed")
    return {
        "status": "approved",
        "decision": "approved",
        "reason": "All rules passed successfully",
        "audit_trail": audit_trail
    }
