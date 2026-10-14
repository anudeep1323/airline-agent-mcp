"""
REST API for apps that know exactly what they want (no LLM). Mounted into airline_service.py under /v1.

  GET   /v1/flights?origin=SFO&destination=LAX&date=2026-10-20   search             200
  GET   /v1/bookings/{confirmation}                             read one booking   200 | 404
  PATCH /v1/bookings/{confirmation}       {"seat": "4D"}        change the seat    200 | 404 | 409 | 422
  POST  /v1/bookings/{confirmation}/cancellations               REQUEST a cancel   202 + approval | 404 | 409
  POST  /v1/approvals/{approval_id}       {"approve": true}     the human decides  200 | 404 | 409

Same business logic as the MCP tools: these call airline_server's functions directly (no copy, no MCP hop).
Every booking call takes customer_id from the verified token (Depends(current_customer)), never from the request body.
"""
import json

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

import airline_server as logic                 # @mcp.tool() returns the plain function, so we can call it
from auth import current_customer

router = APIRouter(prefix="/v1")               # /v1: a future /v2 can change shapes without breaking old apps


def unwrap(tool_output):
    """Tool functions return JSON text with {"error": ...} on failure. REST says it with a STATUS CODE instead."""
    data = json.loads(tool_output)
    if isinstance(data, dict) and "error" in data:
        msg = data["error"]
        # simplification: map by message. A bigger system would raise typed exceptions instead.
        if "not found" in msg:
            raise HTTPException(status_code=404, detail=msg)          # the thing doesn't exist
        if "doesn't exist" in msg:
            raise HTTPException(status_code=422, detail=msg)          # your input is invalid
        raise HTTPException(status_code=409, detail=msg)              # conflicts with current state: taken, already cancelled
    return data


class SeatChange(BaseModel):
    seat: str


class Decision(BaseModel):
    approve: bool


@router.get("/flights")
def search_flights(origin: str, destination: str, date: str, _: str = Depends(current_customer)):
    data = json.loads(logic.search_flights(origin, destination, date))
    return data if isinstance(data, list) else []                     # a search with no hits is 200 + [], not 404


@router.get("/bookings/{confirmation}")
def get_booking(confirmation: str, customer_id: str = Depends(current_customer)):
    return unwrap(logic.get_booking(confirmation, customer_id))


@router.patch("/bookings/{confirmation}")
def change_seat(confirmation: str, body: SeatChange, customer_id: str = Depends(current_customer)):
    return unwrap(logic.change_seat(confirmation, body.seat, customer_id))


@router.post("/bookings/{confirmation}/cancellations", status_code=202)   # 202 = accepted, not done yet
def request_cancellation(confirmation: str, response: Response, customer_id: str = Depends(current_customer)):
    data = unwrap(logic.cancel_booking(confirmation, customer_id))
    response.headers["Location"] = f"/v1/approvals/{data['approval_id']}"   # where to go next
    return data


@router.post("/approvals/{approval_id}")
def decide(approval_id: str, body: Decision, customer_id: str = Depends(current_customer)):
    return unwrap(logic.confirm_cancel(approval_id, body.approve, customer_id))
