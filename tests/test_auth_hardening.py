import ast
import asyncio
import os
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi import HTTPException

import main


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _call_update(authorization):
    return main.update_data({}, authorization=authorization)


def _call_remove(authorization):
    return asyncio.run(
        main.remove_from_watch_list("auth-test-missing", authorization=authorization)
    )


@pytest.mark.parametrize("call", [_call_update, _call_remove])
def test_correct_bearer_token_passes(call):
    assert call(f"Bearer {main.MY_SECRET_TOKEN}")["status"] == "ok"


@pytest.mark.parametrize("call", [_call_update, _call_remove])
@pytest.mark.parametrize(
    "authorization",
    ["Bearer wrong-token", None, "Basic credentials", "Bearer 錯誤"],
    ids=["wrong-token", "missing-header", "non-bearer", "non-ascii-token"],
)
def test_invalid_authorization_returns_401(call, authorization):
    with pytest.raises(HTTPException) as exc_info:
        call(authorization)

    assert exc_info.value.status_code == 401


@pytest.mark.parametrize("token_value", [None, ""], ids=["unset", "empty"])
def test_import_fails_when_api_secret_token_is_missing(tmp_path, token_value):
    env = os.environ.copy()
    if token_value is None:
        env.pop("API_SECRET_TOKEN", None)
    else:
        env["API_SECRET_TOKEN"] = token_value
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    code = (
        "import sys, types; "
        "sys.modules['dotenv'] = types.SimpleNamespace(load_dotenv=lambda: None); "
        "import main"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "RuntimeError: API_SECRET_TOKEN must be set" in result.stderr


def test_api_secret_token_has_no_nonempty_getenv_default():
    tree = ast.parse((PROJECT_ROOT / "main.py").read_text())
    forbidden_calls = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        function = node.func
        is_getenv = (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Name)
            and function.value.id == "os"
            and function.attr == "getenv"
        )
        first_arg = node.args[0]
        default_arg = node.args[1]
        if (
            is_getenv
            and isinstance(first_arg, ast.Constant)
            and first_arg.value == "API_SECRET_TOKEN"
            and isinstance(default_arg, ast.Constant)
            and isinstance(default_arg.value, str)
            and default_arg.value
        ):
            forbidden_calls.append(node.lineno)

    assert forbidden_calls == []
