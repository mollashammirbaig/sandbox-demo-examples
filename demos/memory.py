import os, time

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # install python-dotenv or set env vars manually

from e2b import Sandbox

opts = {
    "api_key": os.environ.get("E2B_API_KEY"),
    "api_url": os.environ.get("E2B_API_URL"),
    "domain":  os.environ.get("E2B_DOMAIN"),
}

sb = Sandbox.create("base", timeout=300, **opts)
time.sleep(3)
print(sb.commands.run("free -m").stdout)
sb.kill()