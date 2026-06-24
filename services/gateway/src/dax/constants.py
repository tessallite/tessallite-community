import os

# Branding defaults from Expert Directive 2.6
SERVER_NAME = os.getenv("XMLA_SERVER_NAME", "Tessallite")
PROVIDER_VERSION = os.getenv("XMLA_PROVIDER_VERSION", "16.0.0.0")

USE_REGEX_PARSER = os.getenv("GATEWAY_USE_REGEX_PARSER", "false").lower() in ("true", "1", "yes")
