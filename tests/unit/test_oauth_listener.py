import asyncio

import httpx
import pytest

from telemetry_nerd.sources.oauth import CallbackListener, OAuthLoginTimeout


async def test_redirect_uri_is_localhost_with_a_live_port():
    async with CallbackListener() as listener:
        assert listener.redirect_uri.startswith("http://127.0.0.1:")
        assert listener.redirect_uri.endswith("/callback")


async def test_wait_for_code_returns_the_callback_code_and_state():
    async with CallbackListener() as listener:

        async def fire():
            async with httpx.AsyncClient() as client:
                await client.get(listener.redirect_uri, params={"code": "abc", "state": "xyz"})

        fired = asyncio.create_task(fire())
        code, state = await listener.wait_for_code(timeout_s=5)
        await fired
        assert (code, state) == ("abc", "xyz")


async def test_wait_for_code_times_out_with_no_callback():
    async with CallbackListener() as listener:
        with pytest.raises(OAuthLoginTimeout):
            await listener.wait_for_code(timeout_s=0.2)


async def test_callback_response_body_tells_the_user_to_close_the_window():
    async with CallbackListener() as listener:

        async def fire():
            async with httpx.AsyncClient() as client:
                return await client.get(
                    listener.redirect_uri, params={"code": "abc", "state": "xyz"}
                )

        task = asyncio.create_task(fire())
        await listener.wait_for_code(timeout_s=5)
        resp = await task
        assert resp.status_code == 200
        assert "close this window" in resp.text.lower()
