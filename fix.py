import re

# Fix agent.py
with open("agent/agent.py", "r", encoding="utf-8") as f:
    content = f.read()

# Pattern: find async def run_agent and move global SESSION to top
old_pattern = r'(async def run_agent\([^)]*\):\n)(    api_key = os\.environ\.get\("GROQ_API_KEY"\))'
new_pattern = r'\1    global SESSION\n\2'

content = re.sub(old_pattern, new_pattern, content)

# Remove inner global SESSION inside async with
content = content.replace(
    '    async with mcp_client.connect(**connect_kwargs) as (session, init_result):\n        global SESSION\n        SESSION = session',
    '    async with mcp_client.connect(**connect_kwargs) as (session, init_result):\n        SESSION = session'
)

with open("agent/agent.py", "w", encoding="utf-8") as f:
    f.write(content)

# Fix planning_agent.py
with open("agent/planning_agent.py", "r", encoding="utf-8") as f:
    content = f.read()

old_pattern = r'(async def run_agent\([^)]*\):\n)(    api_key = os\.environ\.get\("GROQ_API_KEY"\))'
new_pattern = r'\1    global SESSION\n\2'

content = re.sub(old_pattern, new_pattern, content)

content = content.replace(
    '    async with mcp_client.connect(**connect_kwargs) as (session, init_result):\n        global SESSION\n        SESSION = session',
    '    async with mcp_client.connect(**connect_kwargs) as (session, init_result):\n        SESSION = session'
)

with open("agent/planning_agent.py", "w", encoding="utf-8") as f:
    f.write(content)

print("Fixed both files!")