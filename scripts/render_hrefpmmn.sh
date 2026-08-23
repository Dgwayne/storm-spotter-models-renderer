#!/usr/bin/env bash
# Thin wrapper so run-model.sh / workflows can invoke the shared HREF
# sweep without argument plumbing (SCRIPT_FOR maps model -> one script).
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/render_href.sh" HREFPMMN
