"""Run config flow steps the way Home Assistant's flow manager does (AbortFlow becomes a result)."""
from homeassistant.data_entry_flow import AbortFlow


async def step(flow, name, user_input=None):
    try:
        return await getattr(flow, f"async_step_{name}")(user_input)
    except AbortFlow as e:
        return {"type": "abort", "reason": e.reason}
