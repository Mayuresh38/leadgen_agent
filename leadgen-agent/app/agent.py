import os
import json
import re
from typing import Any, AsyncGenerator

from google.adk.agents import LlmAgent
from google.adk.tools import AgentTool
from google.adk.workflow import Workflow, node, START, RetryConfig
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.agents.context import Context
from google.adk.models import Gemini
from google.adk.apps import App, ResumabilityConfig
from pydantic import BaseModel, Field
from google.genai import types

from app.config import config

import sys
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

# --- Define Pydantic Schemas for Structured Output ---

class LeadDetails(BaseModel):
    sender_name: str = Field(description="Name of the person who sent the email")
    sender_email: str = Field(description="Email address of the sender")
    company_name: str = Field(description="Name of the company or organization")
    industry: str = Field(description="Industry of the company (e.g. Technology, Healthcare, Finance, etc.)")
    inquiry: str = Field(description="Brief summary of the lead's inquiry or request")

class LeadScore(BaseModel):
    score: int = Field(description="Score from 0 to 100 based on lead criteria")
    urgency: str = Field(description="Urgency of the lead (High, Medium, Low)")
    reasoning: str = Field(description="Reasoning for the assigned score")

class LeadAnalysis(BaseModel):
    details: LeadDetails = Field(description="Extracted lead details")
    score_info: LeadScore = Field(description="Calculated score and reasoning")


# --- Define MCP CRM Toolset ---

mcp_script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_server.py")

crm_mcp_toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command=sys.executable,
            args=[mcp_script_path],
        )
    )
)


# --- Configure Retries for Transient Errors ---
transient_retry_config = RetryConfig(
    max_attempts=5,
    initial_delay=10.0,
    backoff_factor=2.0,
)

gemini_model = Gemini(
    model=config.model,
    retry_options=types.HttpRetryOptions(
        initial_delay=10.0,
        attempts=8,
    ),
)

# --- Define Specialized Sub-Agents ---

email_parser = LlmAgent(
    name="email_parser",
    model=gemini_model,
    retry_config=transient_retry_config,
    instruction=(
        "You are a specialized email parsing agent. Parse the lead email and extract:\n"
        "1. Sender name\n"
        "2. Sender email\n"
        "3. Company name\n"
        "4. Industry\n"
        "5. Brief summary of the inquiry\n"
        "Format the output exactly matching the LeadDetails schema."
    ),
    output_schema=LeadDetails,
)

lead_scorer = LlmAgent(
    name="lead_scorer",
    model=gemini_model,
    retry_config=transient_retry_config,
    instruction=(
        "You are a specialized lead scoring agent. Analyze the lead details and score the lead from 0 to 100.\n"
        "First, use the get_company_profile tool with the company name to fetch details like size and industry.\n"
        "Score criteria:\n"
        "- Tech, Healthcare, and Finance industries get +20 points.\n"
        "- Clear purchasing intent or immediate needs gets +30 points.\n"
        "- Specific budget or company size details mentioned gets +20 points.\n"
        "- Generic, spam, or out-of-scope inquiries get low scores (<40 points).\n"
        "Format the output exactly matching the LeadScore schema."
    ),
    tools=[crm_mcp_toolset],
    output_schema=LeadScore,
)

# --- Define Orchestrator Agent ---

lead_coordinator = LlmAgent(
    name="lead_coordinator",
    model=gemini_model,
    retry_config=transient_retry_config,
    instruction=(
        "You are the Lead Coordinator. Coordinate the analysis of an inbound lead email.\n"
        "1. Call the email_parser tool with the raw email text.\n"
        "2. Call the lead_scorer tool with the extracted lead details.\n"
        "3. Check if the lead exists in the CRM by calling search_crm with the sender's email.\n"
        "4. Save the lead in the CRM by calling create_or_update_crm_lead with all lead details, "
        "the score, and status: 'Approved' (if score >= 70), 'Needs Review' (if 40 <= score < 70), "
        "or 'Rejected' (if score < 40). You must call this for EVERY lead.\n"
        "5. Finally, you MUST return the final structured response matching the LeadAnalysis output schema "
        "by calling the set_model_response tool. Do not skip this step under any circumstances!"
    ),
    tools=[AgentTool(email_parser), AgentTool(lead_scorer), crm_mcp_toolset],
    output_schema=LeadAnalysis,
)


# --- Define Workflow Nodes ---

def security_checkpoint(ctx: Context, node_input: types.Content | str) -> Event:
    """Security node to check for PII, prompt injections, and generate an audit log."""
    import datetime
    
    # Obtain raw text
    if isinstance(node_input, str):
        email_text = node_input
    elif hasattr(node_input, "parts") and node_input.parts:
        email_text = node_input.parts[0].text or ""
    else:
        email_text = str(node_input)

    # 1. PII Scrubbing
    # Regex for SSN (e.g. 000-00-0000) and Credit Card numbers
    ssn_pattern = r'\b\d{3}-\d{2}-\d{4}\b'
    cc_pattern = r'\b(?:\d[ -]*?){13,16}\b'
    
    scrubbed_text = re.sub(ssn_pattern, "[REDACTED_SSN]", email_text)
    scrubbed_text = re.sub(cc_pattern, "[REDACTED_CC]", scrubbed_text)
    
    # Save the scrubbed email text in state
    ctx.state["raw_email"] = scrubbed_text
    
    # Audit log structure
    audit_log = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "session_id": ctx.session.id,
        "run_id": ctx.run_id,
        "event": "security_checkpoint_evaluation",
        "pii_scrubbed": scrubbed_text != email_text,
    }

    # 2. Domain-Specific Rule: Competitor Blocking
    competitor_domains = ["competitor.com", "rivalcorp.com", "badlead.com"]
    email_matches = re.findall(r'[\w\.-]+@[\w\.-]+', email_text)
    competitor_detected = False
    for email in email_matches:
        domain = email.split("@")[-1].lower()
        if domain in competitor_domains:
            competitor_detected = True
            break
            
    if competitor_detected:
        audit_log.update({
            "severity": "WARNING",
            "status": "BLOCKED",
            "reason": "Competitor domain detected in email content"
        })
        print(f"AUDIT LOG: {json.dumps(audit_log)}", flush=True)
        return Event(output="Security Block: Leads from competitor domains are not permitted.", route="SECURITY_EVENT")

    # 3. Prompt Injection Detection
    injection_keywords = [
        "ignore previous instructions", 
        "disregard instructions", 
        "ignore above", 
        "ignore the instructions", 
        "override", 
        "you are now in developer mode", 
        "jailbreak", 
        "system prompt",
        "forget all instructions"
    ]
    
    injection_detected = False
    text_lower = email_text.lower()
    for kw in injection_keywords:
        if kw in text_lower:
            injection_detected = True
            break
            
    if injection_detected:
        audit_log.update({
            "severity": "CRITICAL",
            "status": "BLOCKED",
            "reason": "Possible prompt injection attack detected"
        })
        print(f"AUDIT LOG: {json.dumps(audit_log)}", flush=True)
        return Event(output="Security Block: Potential prompt injection attempt detected.", route="SECURITY_EVENT")

    # If all checks pass
    audit_log.update({
        "severity": "INFO",
        "status": "SAFE",
        "reason": "No security violations detected"
    })
    print(f"AUDIT LOG: {json.dumps(audit_log)}", flush=True)
    return Event(output=scrubbed_text, route="SAFE")


def routing_node(ctx: Context, node_input: Any) -> Event:
    """Evaluates the lead score and routes the workflow accordingly."""
    # Ensure node_input is a dictionary
    if not isinstance(node_input, dict):
        text_content = ""
        if hasattr(node_input, "parts") and node_input.parts:
            text_content = "".join(p.text for p in node_input.parts if hasattr(p, "text") and p.text)
        else:
            text_content = str(node_input)
            
        node_input = {
            "details": {
                "sender_name": "Unknown",
                "sender_email": "Unknown",
                "company_name": "Unknown",
                "industry": "Unknown",
                "inquiry": text_content or "Failed to parse lead details."
            },
            "score_info": {
                "score": 0,
                "urgency": "Low",
                "reasoning": f"Lead coordinator stopped without generating structured analysis. Raw content: {text_content}"
            }
        }

    # Save coordination result to state
    ctx.state["analysis_result"] = node_input
    
    score = node_input.get("score_info", {}).get("score", 0)
    
    if 40 <= score < 70:
        return Event(output=node_input, route="needs_review")
    elif score >= 70:
        return Event(output=node_input, route="auto_approve")
    else:
        return Event(output=node_input, route="auto_reject")


@node(rerun_on_resume=True)
async def human_review(ctx: Context, node_input: dict) -> AsyncGenerator[Any, None]:
    """Human-in-the-loop pause for borderline leads."""
    if not ctx.resume_inputs or "approval" not in ctx.resume_inputs:
        score = node_input.get("score_info", {}).get("score", 0)
        yield RequestInput(
            interrupt_id="approval",
            message=f"Lead score is borderline ({score}/100). Please approve (yes) or reject (no)."
        )
        return
        
    decision = ctx.resume_inputs["approval"]
    is_approved = str(decision).strip().lower() in ("yes", "y", "approve", "approved")
    
    # Update score reasoning
    node_input["score_info"]["reasoning"] += f" | Human Review: {'Approved' if is_approved else 'Rejected'}"
    if not is_approved:
        node_input["score_info"]["score"] = 0
        
    yield Event(output=node_input)


def final_output(ctx: Context, node_input: dict | str) -> Event:
    """Formats and prints the final report or security event."""
    if isinstance(node_input, str):
        # Security event or direct message block
        yield Event(
            content=types.Content(role="model", parts=[types.Part.from_text(text=f"⚠️ {node_input}")]),
            output={"status": "blocked", "message": node_input}
        )
        return

    details = node_input.get("details", {})
    score_info = node_input.get("score_info", {})
    
    score = score_info.get("score", 0)
    urgency = score_info.get("urgency", "N/A")
    reasoning = score_info.get("reasoning", "N/A")
    
    output_text = (
        f"### 📋 Lead Analysis Report\n\n"
        f"**Sender**: {details.get('sender_name', 'N/A')} ({details.get('sender_email', 'N/A')})\n"
        f"**Company**: {details.get('company_name', 'N/A')} | **Industry**: {details.get('industry', 'N/A')}\n"
        f"**Inquiry Summary**: {details.get('inquiry', 'N/A')}\n\n"
        f"**Score**: {score}/100 | **Urgency**: {urgency}\n"
        f"**Reasoning**: {reasoning}\n\n"
    )
    
    if score >= 70:
        output_text += "🟢 **Action**: Lead auto-approved and queued for priority outreach!"
    elif 40 <= score < 70:
        output_text += "🟡 **Action**: Lead manually approved after human review."
    else:
        output_text += "🔴 **Action**: Lead rejected."
        
    yield Event(content=types.Content(role="model", parts=[types.Part.from_text(text=output_text)]))
    yield Event(output=node_input)


# --- Create Workflow ---

workflow = Workflow(
    name="leadgen_workflow",
    edges=[
        (START, security_checkpoint),
        (security_checkpoint, {"SAFE": lead_coordinator, "SECURITY_EVENT": final_output}),
        (lead_coordinator, routing_node),
        (routing_node, {"needs_review": human_review, "__DEFAULT__": final_output}),
        (human_review, final_output),
    ],
    description="Automated lead parsing, scoring, and routing workflow.",
)

app = App(
    name="app",
    root_agent=workflow,
    resumability_config=ResumabilityConfig(is_resumable=True),
)
