from telemetry import setup_tracing

setup_tracing()  # must run BEFORE any google.adk import

from google.adk.cli.fast_api import get_fast_api_app
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

app = get_fast_api_app(agents_dir=".", web=False)
FastAPIInstrumentor.instrument_app(app)  # reads the incoming traceparent header