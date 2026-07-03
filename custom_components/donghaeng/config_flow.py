"""동행 복권 통합 모듈을 위한 설정 흐름."""

import logging
from typing import Any

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow, ConfigEntry
from homeassistant.core import callback
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from .client.dh_lottery_client import DhLotteryClient, DhLotteryError
from .const import DOMAIN, TITLE, CONF_LOTTO_645, CONF_PENSION_720

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_LOTTO_645, default=True): cv.boolean,
        vol.Optional(CONF_PENSION_720, default=True): cv.boolean,
        vol.Optional("pension_buy_time", default="17:00"): cv.string,
        vol.Optional("pension_buy_weekday", default="목요일"): vol.In(
            ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]
        ),
        vol.Optional("discord_webhook_url", default=""): cv.string,
    }
)


async def async_validate_login(username: str, password: str) -> dict[str, Any]:
    """사용자 입력을 검증하여 연결할 수 있는지 확인합니다.
    Data는 STEP_USER_DATA_SCHEMA로부터 키 값을 갖고 있습니다.
    """
    client = DhLotteryClient(username, password)

    errors = {}
    try:
        await client.async_login()
    except DhLotteryError as ex:
        _LOGGER.exception("동행 복권 로그인 실패: %s", ex)
        errors["base"] = "invalid_login"
    finally:
        await client.session.close()
    return errors


class DhLotteryConfigFlow(ConfigFlow, domain=DOMAIN):
    """동행 복권 설정 흐름을 처리합니다."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OptionsFlow:
        """옵션 흐름 등록"""
        return DhLotteryOptionsFlowHandler(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """초기 단계 처리"""

        if user_input is None:
            return self.async_show_form(
                step_id="user", data_schema=STEP_USER_DATA_SCHEMA
            )

        username = user_input[CONF_USERNAME]
        password = user_input[CONF_PASSWORD]

        if errors := await async_validate_login(username, password):
            return self.async_show_form(
                step_id="user",
                data_schema=STEP_USER_DATA_SCHEMA,
                errors=errors,
            )
        await self.async_set_unique_id(f"{DOMAIN}_{username}")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=f"{TITLE} ({username})", data=user_input)


class DhLotteryOptionsFlowHandler(OptionsFlow):
    """옵션 설정을 관리하는 핸들러입니다."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """초기화"""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """옵션 설정의 진입점"""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        data = self.config_entry.data

        discord_webhook_url = options.get("discord_webhook_url", data.get("discord_webhook_url", ""))
        pension_buy_time = options.get("pension_buy_time", data.get("pension_buy_time", "17:00"))
        pension_buy_weekday = options.get("pension_buy_weekday", data.get("pension_buy_weekday", "목요일"))
        pension_manual_numbers = options.get("pension_manual_numbers", "810212,810410,120911,150402")

        schema = vol.Schema(
            {
                vol.Optional("discord_webhook_url", default=discord_webhook_url): cv.string,
                vol.Optional("pension_buy_time", default=pension_buy_time): cv.string,
                vol.Optional("pension_buy_weekday", default=pension_buy_weekday): vol.In(
                    ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]
                ),
                vol.Optional("pension_manual_numbers", default=pension_manual_numbers): cv.string,
            }
        )

        return self.async_show_form(
            step_id="init", data_schema=schema
        )
