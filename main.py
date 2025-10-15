import asyncio
import base64
import json
import websockets
import os
from dotenv import load_dotenv
from app.pharmacy_functions import FUNCTION_MAP
from urllib.parse import parse_qs
import ssl
import certifi


load_dotenv()

def sts_connect():
    api_key = os.getenv("DEEPGRAM_API_KEY")
    if not api_key:
        raise Exception("DEEPGRAM_API_KEY not found")

    print(f"Connecting to Deepgram with API key: {api_key[:10]}...")

    ssl_context = ssl.create_default_context(cafile=certifi.where())

    sts_ws = websockets.connect(
        "wss://agent.deepgram.com/v1/agent/converse",
        subprotocols=["token", api_key],
        ssl=ssl_context,
        open_timeout=10,  # Increase timeout
        close_timeout=10
    )
    return sts_ws

async def _safe_close_twilio(twilio_ws, conversation):
    try:
        if conversation.get("_ws_closed"):
            return
        conversation["_ws_closed"] = True
        try:
            await twilio_ws.close()
        except Exception:
            pass
    except Exception:
        pass

async def _enforce_call_timeout(max_seconds, twilio_ws, conversation):
    try:
        if not max_seconds or max_seconds <= 0:
            return
        await asyncio.sleep(max_seconds)
        # Mark timeout and persist conversation before closing
        try:
            conversation.setdefault("events", []).append({
                "type": "timeout",
                "max_seconds": max_seconds,
                "timestamp": __import__('datetime').datetime.utcnow().isoformat() + "Z"
            })
            conversation["ended_at"] = __import__('datetime').datetime.utcnow().isoformat() + "Z"
            await save_conversation(conversation)
        except Exception as e:
            print(f"Failed saving conversation on timeout: {e}")
        await _safe_close_twilio(twilio_ws, conversation)
    except asyncio.CancelledError:
        # Normal on successful call end before timeout
        return

def load_agent_config(agent_id):
    """Load agent-specific config"""
    config_path = f"agents/{agent_id}.json"
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            return json.load(f)
    else:
        # Fallback to default config
        with open("config.json", "r") as f:
            return json.load(f)

async def handle_barge_in(decoded, twilio_ws, streamsid):
    if decoded["type"] == "UserStartedSpeaking":
        clear_message = {
            "event": "clear",
            "streamSid": streamsid
        }
        await twilio_ws.send(json.dumps(clear_message))

def execute_function_call(func_name, arguments, agent_config=None, conversation=None):
    if func_name in FUNCTION_MAP:
        # Pass agent config to functions that might need it
        if agent_config and hasattr(FUNCTION_MAP[func_name], '__code__'):
            # Check if function accepts agent_config parameter
            func_code = FUNCTION_MAP[func_name].__code__
            if 'agent_config' in func_code.co_varnames:
                arguments['agent_config'] = agent_config
        
        # Pass conversation to transfer function
        if func_name == 'transfer_to_human':
            arguments['conversation'] = conversation
        
        # Auto-add caller's phone number for appointment booking
        if func_name == 'book_appointment':
            if conversation and 'caller_phone' in conversation:
                if 'phone_number' not in arguments or not arguments.get('phone_number'):
                    arguments['phone_number'] = conversation['caller_phone']
                    print(f"✅ Auto-added caller phone number: {conversation['caller_phone']}")
                
                # Also add agent phone number for SMS sending
                if 'agent_phone' in conversation:
                    arguments['agent_phone_number'] = conversation['agent_phone']
                    print(f"✅ Auto-added agent phone number: {conversation['agent_phone']}")
            else:
                print("❌ No caller phone number available")
        
        result = FUNCTION_MAP[func_name](**arguments)
        print(f"Function call result: {result}")
        return result
    else:
        result = {"error": f"Unknown function: {func_name}"}
        print(result)
        return result

def create_function_call_response(func_id, func_name, result):
    return {
        "type": "FunctionCallResponse",
        "id": func_id,
        "name": func_name,
        "content": json.dumps(result)
    }

async def handle_call_transfer(transfer_number, conversation):
    """Handle call transfer using Twilio REST API"""
    try:
        # Import Twilio client
        from twilio.rest import Client
        
        # Get Twilio credentials from environment
        account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        
        if not account_sid or not auth_token:
            print("❌ Twilio credentials not configured - cannot transfer call")
            return False
        
        # Get call SID from conversation
        # We need to capture this from Twilio's start event
        call_sid = conversation.get("call_sid")
        
        if not call_sid:
            print("❌ No call SID available - cannot transfer call")
            print("⚠️ Note: Call SID must be captured from Twilio start event")
            return False
        
        print(f"🔄 Initiating transfer to {transfer_number} for call {call_sid}")
        
        # Create Twilio client
        client = Client(account_sid, auth_token)
        
        # Generate TwiML for transfer
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Please hold while I transfer you to a human agent.</Say>
    <Dial timeout="30" action="">{transfer_number}</Dial>
    <Say>I'm sorry, the transfer could not be completed. Please try again later. Goodbye.</Say>
</Response>"""
        
        # Update the call with new TwiML to perform transfer
        call = client.calls(call_sid).update(twiml=twiml)
        
        print(f"✅ Call transferred successfully to {transfer_number}")
        
        # Log transfer in conversation history
        conversation.setdefault("events", []).append({
            "type": "transfer",
            "timestamp": __import__('datetime').datetime.utcnow().isoformat() + "Z",
            "transfer_to": transfer_number,
            "call_sid": call_sid,
            "status": "initiated"
        })
        
        return True
        
    except Exception as e:
        print(f"❌ Transfer failed: {e}")
        import traceback
        traceback.print_exc()
        return False

async def handle_function_call_request(decoded, sts_ws, agent_config, conversation):
    try:
        for function_call in decoded["functions"]:
            func_name = function_call["name"]
            func_id = function_call["id"]
            arguments = json.loads(function_call["arguments"])

            print(f"Function call: {func_name} (ID: {func_id}), arguments: {arguments}")

            result = execute_function_call(func_name, arguments, agent_config, conversation)
            
            # Check if this is a transfer request
            if result.get("action") == "transfer" and result.get("success"):
                transfer_number = result.get("transfer_to")
                if transfer_number:
                    print(f"🔄 Transfer action detected - transferring to {transfer_number}")
                    # Attempt the transfer
                    transfer_success = await handle_call_transfer(transfer_number, conversation)
                    if transfer_success:
                        result["transfer_status"] = "initiated"
                    else:
                        result["transfer_status"] = "failed"
                        result["message"] = "I apologize, but I was unable to complete the transfer. Please try calling our main office number."

            function_result = create_function_call_response(func_id, func_name, result)
            await sts_ws.send(json.dumps(function_result))
            print(f"Sent function result: {function_result}")

    except Exception as e:
        print(f"Error calling function: {e}")
        error_result = create_function_call_response(
            func_id if "func_id" in locals() else "unknown",
            func_name if "func_name" in locals() else "unknown",
            {"error": f"Function call failed with: {str(e)}"}
        )
        await sts_ws.send(json.dumps(error_result))

async def handle_text_message(decoded, twilio_ws, sts_ws, streamsid, agent_config, conversation):
    await handle_barge_in(decoded, twilio_ws, streamsid)

    if decoded["type"] == "FunctionCallRequest":
        await handle_function_call_request(decoded, sts_ws, agent_config, conversation)

async def sts_sender(sts_ws, audio_queue):
    print("sts_sender started")
    while True:
        chunk = await audio_queue.get()
        await sts_ws.send(chunk)

async def sts_receiver(sts_ws, twilio_ws, streamsid_queue, agent_config, conversation):
    print("sts_receiver started")
    streamsid = await streamsid_queue.get()

    async for message in sts_ws:
        if type(message) is str:
            print(message)
            decoded = json.loads(message)
            await handle_text_message(decoded, twilio_ws, sts_ws, streamsid, agent_config, conversation)
            try:
                # Append decoded text events to conversation log
                conversation.setdefault("events", []).append({
                    "source": "deepgram",
                    "timestamp": conversation.get("_now_func", __import__('datetime').datetime.utcnow)().isoformat() + "Z",
                    "payload": decoded
                })
            except Exception:
                pass
            continue

        raw_mulaw = message

        media_message = {
            "event": "media",
            "streamSid": streamsid,
            "media": {"payload": base64.b64encode(raw_mulaw).decode("ascii")}
        }

        await twilio_ws.send(json.dumps(media_message))

async def twilio_receiver(twilio_ws, audio_queue, streamsid_queue, agent_queue, conversation):
    BUFFER_SIZE = 20 * 160
    inbuffer = bytearray(b"")
    timeout_deadline = None

    async for message in twilio_ws:
        try:
            data = json.loads(message)
            event = data["event"]

            if event == "start":
                print("get our streamsid")
                start = data["start"]
                streamsid = start["streamSid"]
                streamsid_queue.put_nowait(streamsid)
                try:
                    conversation["streamSid"] = streamsid
                except Exception:
                    pass
                # Establish per-call timeout deadline if configured
                try:
                    max_secs = int(conversation.get("_max_call_seconds") or 0)
                    if max_secs > 0:
                        from datetime import datetime, timedelta
                        timeout_deadline = datetime.utcnow() + timedelta(seconds=max_secs)
                        print(f"⏱️ Call timeout deadline set for {timeout_deadline.isoformat()}Z (in {max_secs}s)")
                except Exception as e:
                    print(f"Failed to set timeout deadline: {e}")
                
                # Capture Call SID for transfer functionality
                call_sid = start.get("callSid")
                if call_sid:
                    conversation["call_sid"] = call_sid
                    print(f"✅ Call SID captured: {call_sid}")
                else:
                    print("⚠️ No Call SID found in start event (transfer will not work)")
                    
                # Read customParameters (includes agent and caller phone)
                custom_params = start.get("customParameters") or {}
                if custom_params:
                    print(f"Twilio customParameters: {custom_params}")
                    
                    # Get agent from parameters
                    agent_from_params = custom_params.get("agent")
                    if agent_from_params:
                        try:
                            agent_queue.put_nowait(agent_from_params)
                        except Exception:
                            pass
                    
                    # Get caller phone number from parameters
                    caller_phone = custom_params.get("from")
                    if caller_phone:
                        conversation["caller_phone"] = caller_phone
                        print(f"✅ Caller phone number captured: {caller_phone}")
                    else:
                        print("❌ No caller phone number found in customParameters")
                    
                    # Also capture the agent's phone number (the To number in the call)
                    # This will be used for SMS confirmations to send from the correct number
                    agent_phone = custom_params.get("to")
                    if agent_phone:
                        conversation["agent_phone"] = agent_phone
                        print(f"✅ Agent phone number captured: {agent_phone}")
                    else:
                        print("❌ No agent phone number found in customParameters")
                else:
                    print("❌ No customParameters found in start event")
            elif event == "connected":
                continue
            elif event == "media":
                media = data["media"]
                chunk = base64.b64decode(media["payload"])
                if media["track"] == "inbound":
                    inbuffer.extend(chunk)
            # Check timeout on each received event
            try:
                if timeout_deadline is not None:
                    from datetime import datetime
                    if datetime.utcnow() >= timeout_deadline:
                        print("⏹️ Max call duration reached inside receiver; ending call")
                        try:
                            conversation["ended_at"] = __import__('datetime').datetime.utcnow().isoformat() + "Z"
                            conversation.setdefault("events", []).append({
                                "type": "timeout",
                                "reached_via": "receiver",
                                "timestamp": __import__('datetime').datetime.utcnow().isoformat() + "Z"
                            })
                            await save_conversation(conversation)
                        except Exception as e:
                            print(f"Failed saving conversation on receiver-timeout: {e}")
                        await _safe_close_twilio(twilio_ws, conversation)
                        break
            except Exception:
                pass
            if event == "stop":
                try:
                    conversation["ended_at"] = __import__('datetime').datetime.utcnow().isoformat() + "Z"
                    # Minimal save on stop
                    await save_conversation(conversation)
                except Exception as e:
                    print(f"Failed saving conversation: {e}")
                break

            while len(inbuffer) >= BUFFER_SIZE:
                chunk = inbuffer[:BUFFER_SIZE]
                audio_queue.put_nowait(chunk)
                inbuffer = inbuffer[BUFFER_SIZE:]
        except:
            break


async def twilio_handler(twilio_ws):
    # Parse query parameters from the WebSocket URL (websockets v12+)
    ws_path = getattr(twilio_ws, "path", "") or ""
    print(f"Incoming WS path: {ws_path}")
    query_params = parse_qs(ws_path.split('?')[-1] if '?' in ws_path else '')
    agent_id_qs = query_params.get('agent', [''])[0] or None

    audio_queue = asyncio.Queue()
    streamsid_queue = asyncio.Queue()
    agent_queue = asyncio.Queue()

    # Start Twilio receiver first to capture start.customParameters.agent
    # Prepare conversation log container
    from datetime import datetime
    conversation = {
        "agent_id": None,
        "streamSid": None,
        "started_at": datetime.utcnow().isoformat() + "Z",
        "ended_at": None,
        "events": []
    }
    twilio_receiver_task = asyncio.create_task(twilio_receiver(twilio_ws, audio_queue, streamsid_queue, agent_queue, conversation))

    # Determine agent_id: prefer customParameters, fallback to query, else default
    agent_id = agent_id_qs
    try:
        # Wait longer for agent from start event (customParameters)
        agent_id_from_start = await asyncio.wait_for(agent_queue.get(), timeout=5.0)
        if agent_id_from_start:
            agent_id = agent_id_from_start
            print(f"Agent from customParameters: {agent_id}")
    except asyncio.TimeoutError:
        print("Timeout waiting for agent from customParameters")
        pass

    if not agent_id:
        agent_id = 'default'

    print(f"Connecting agent: {agent_id}")
    try:
        conversation["agent_id"] = agent_id
    except Exception:
        pass

    # Load agent-specific config
    agent_config = load_agent_config(agent_id)
    # Tag local agent_id for calendar/cache logic
    try:
        agent_config["agent_id"] = agent_id
    except Exception:
        pass

    # Build a Deepgram-safe config (strip local-only keys)
    allowed_keys = {"type", "audio", "agent"}
    deepgram_config = {k: v for k, v in agent_config.items() if k in allowed_keys}

    # Inject current date/time into the system prompt using agent timezone (or UTC)
    try:
        from datetime import datetime
        import pytz
        tz_name = ((agent_config.get("google_calendar") or {}).get("timezone")
                   or "UTC")
        tz = pytz.timezone(tz_name)
        now = datetime.now(tz)
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%I:%M %p %Z")
        iso_str = now.isoformat()
        agent_obj = deepgram_config.get("agent") or {}
        think_obj = agent_obj.get("think") or {}
        prompt = think_obj.get("prompt") or ""
        if prompt:
            prompt = (prompt
                      .replace("{{CURRENT_DATE}}", date_str)
                      .replace("{{CURRENT_TIME}}", time_str)
                      .replace("{{NOW_ISO}}", iso_str)
                      .replace("{{CURRENT_TZ}}", tz_name)
                      .replace("currdate", date_str)
                      .replace("Current date: {{CURRENT_DATE}}", f"Current date: {date_str}"))
            deepgram_config.setdefault("agent", {}).setdefault("think", {})["prompt"] = prompt
    except Exception as e:
        print(f"Prompt time injection failed (non-fatal): {e}")

    # Log a brief preview of the prompt and greeting being sent
    try:
        preview_prompt = (deepgram_config.get("agent", {}).get("think", {}).get("prompt") or "")[:160]
        preview_greeting = (deepgram_config.get("agent", {}).get("greeting") or "")[:120]
        print(f"Agent {agent_id} prompt preview: {preview_prompt}")
        print(f"Agent {agent_id} greeting: {preview_greeting}")
    except Exception:
        pass

    max_call_seconds = 1000

    timeout_task = None

    # Stash max seconds onto conversation for receiver-side enforcement
    if max_call_seconds > 0:
        conversation["_max_call_seconds"] = max_call_seconds

    async with sts_connect() as sts_ws:
        await sts_ws.send(json.dumps(deepgram_config))

        # Start timeout enforcement after Deepgram is connected
        if max_call_seconds > 0:
            print(f"⏱️ Enforcing max call duration: {max_call_seconds} seconds")
            timeout_task = asyncio.create_task(_enforce_call_timeout(max_call_seconds, twilio_ws, conversation))

        sender_task = asyncio.ensure_future(sts_sender(sts_ws, audio_queue))
        receiver_task = asyncio.ensure_future(sts_receiver(sts_ws, twilio_ws, streamsid_queue, agent_config, conversation))

        wait_tasks = [sender_task, receiver_task, twilio_receiver_task]
        if timeout_task:
            wait_tasks.append(timeout_task)

        done, pending = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)

        # Cancel remaining tasks
        for task in pending:
            try:
                task.cancel()
            except Exception:
                pass

        await _safe_close_twilio(twilio_ws, conversation)

async def main():
    await websockets.serve(twilio_handler, "localhost", 5000)
    print("Started server.")
    await asyncio.Future()

# Utilities for saving conversation history
async def save_conversation(conversation):
    try:
        import os
        import json
        from datetime import datetime
        agent_id = conversation.get("agent_id") or "unknown"
        started_at = conversation.get("started_at") or datetime.utcnow().isoformat() + "Z"
        # Sanitize filename elements
        ts = started_at.replace(":", "-")
        stream_sid = conversation.get("streamSid") or "nostream"
        dir_path = os.path.join("history", agent_id)
        os.makedirs(dir_path, exist_ok=True)
        file_path = os.path.join(dir_path, f"{ts}__{stream_sid}.json")
        # Write pretty JSON
        def _write():
            with open(file_path, "w") as f:
                json.dump(conversation, f, indent=2)
        await asyncio.to_thread(_write)
    except Exception as e:
        print(f"Error writing conversation history: {e}")

if __name__ == "__main__":
    asyncio.run(main())