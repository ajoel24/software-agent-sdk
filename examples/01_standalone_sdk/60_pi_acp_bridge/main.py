"""OpenHands SDK — Pi ACP Bridge Example.

Demonstrates using pi as an ACP-compatible agent backend via ACPAgent.
The bridge script (pi_acp_bridge.py) exposes pi's RPC mode as an ACP
server that OpenHands can delegate to.

Requirements:
  - pi installed and on PATH
  - LLM_API_KEY environment variable set
"""

import os
import shutil
import tempfile
from pathlib import Path

from openhands.sdk import Conversation, get_logger
from openhands.sdk.agent import ACPAgent


logger = get_logger(__name__)

BRIDGE_SCRIPT = Path(__file__).parent / "pi_acp_bridge.py"

api_key = os.getenv("LLM_API_KEY")
assert api_key is not None, "LLM_API_KEY environment variable is not set."

if shutil.which("pi") is None:
    raise RuntimeError("'pi' not found on PATH. Install pi first.")

with tempfile.TemporaryDirectory() as tmpdir:
    workspace = Path(tmpdir)
    (workspace / "hello.py").write_text('print("hello")\n')

    agent = ACPAgent(
        acp_command=["python", str(BRIDGE_SCRIPT)],
    )
    conversation = Conversation(agent=agent, workspace=str(workspace))
    conversation.send_message("Read hello.py and add a function that returns 'world'.")
    conversation.run()

    content = (workspace / "hello.py").read_text()
    print(f"\n[hello.py]\n{content}")

    cost = conversation.conversation_stats.get_combined_metrics().accumulated_cost
    print(f"\nEXAMPLE_COST: {cost}")
