import json
import os
import uuid
from pathlib import Path
 
import litellm
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel
 
from tools import TOOLS, run_tool
 
# --- Config ---
 
SYSTEM_PROMPT = (
    "You are Pickle, an after-work pickleball planner for New York. "
    "Whenever the user gives a location, your first action must be to call find_pickleball_courts "
    "and find the 3 closest courts unless they ask for a different amount. Never answer a location with get_sun_times alone. That one "
    "call returns the three nearest courts plus the current local time, sunset times and is_dark_now. "
    "Use can_i_play_before_dark only when the user says how they will travel or asks if they can "
    "make it before dark. Use get_sun_times only for questions that are just about sunrise or sunset. "
    "Use get_distance_to_court when the user asks how far a court is from them or from an address. "
    "Pass a court's lat_lon as court_location. If the user has not given a location, ask for one. "
    "Courts are ranked by travel distance along streets, not straight-line; if the user says how "
    "they travel, pass it as mode to find_pickleball_courts, otherwise it assumes walking. "
    "The page shows each court on a map card, so do not list them again. Say which court is "
    "closest by travel distance and how far it is. Then, if is_dark_now is true, say it is late out and already dark, "
    "give the time sunset was, and give tomorrow's sunrise and sunset. If it is still light, say roughly how "
    "much daylight is left. Mention lights only if known. If a travel time is an estimate, say it "
    "is approximate. Write plain text only, with no markdown. Keep replies to 2 or 3 sentences."
)
 
MAX_TOOL_ROUNDS = 5
 
 
# --- The Harness ---
 
def run_agent(messages: list[dict]) -> tuple[str, list[dict]]:
    """Complete until the model answers without asking for a tool.
 
    Returns the final text and a record of every tool call made along the way.
    """
    tool_calls = []
 
    for _ in range(MAX_TOOL_ROUNDS):
        reply = litellm.completion(
            model="vertex_ai/gemini-3.5-flash-lite",
            vertex_location="global",
            messages=messages,
            tools=TOOLS,
        ).choices[0].message
 
        # Save the assistant's response to the conversation history.
        messages += [reply.model_dump()]
 
        if not reply.tool_calls:
            return reply.content, tool_calls
 
        # Run each requested tool and save its result to the conversation history.
        for call in reply.tool_calls:
            args = json.loads(call.function.arguments)
            result = run_tool(call.function.name, args)
 
            tool_calls += [{
                "name": call.function.name,
                "args": args,
                "result": result
            }]
 
            messages += [{
                "role": "tool",
                "tool_call_id": call.id,
                "content": result
            }]
 
    return "Sorry, I hit my tool-call limit before finishing.", tool_calls
 
 
# --- Session Store ---
 
# Each session_id has its own complete conversation history.
# This allows the agent to remember previous messages and tool results
# during the current session.
sessions: dict[str, list] = {}
 
 
# --- FastAPI App ---
 
app = FastAPI()
 
 
class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
 
 
class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls: list[dict]
 
 
@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "index.html")
 
 
@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    # Get the existing session or create a new one.
    session_id = request.session_id or str(uuid.uuid4())
 
    if session_id not in sessions:
        sessions[session_id] = [{
            "role": "system",
            "content": SYSTEM_PROMPT
        }]
 
    # Add the new user message to this session's history.
    sessions[session_id] += [{
        "role": "user",
        "content": request.message
    }]
 
    try:
        response, tool_calls = run_agent(sessions[session_id])
    except Exception as e:
        response, tool_calls = (
            f"Model call failed: {type(e).__name__}: {str(e)[:300]}",
            []
        )
 
    return ChatResponse(
        response=response,
        session_id=session_id,
        tool_calls=tool_calls
    )
 
 
@app.post("/clear")
def clear(session_id: str | None = None):
    sessions.pop(session_id, None)
    return {"status": "ok"}
 
 
if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000))
    )
