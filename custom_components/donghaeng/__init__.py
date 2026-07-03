"""동행 복권 통합 모듈"""

import logging
from dataclasses import dataclass
from typing import Optional, List

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD
from homeassistant.core import (
    HomeAssistant,
    ServiceResponse,
    ServiceCall,
    SupportsResponse,
)
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er

from .client.dh_lottery_client import DhLotteryClient, DhLotteryError
from .client.dh_lotto_645 import DhLotto645SelMode, DhLotto645
from .const import (
    DOMAIN,
    PLATFORMS,
    CONF_LOTTO_645,
    CONF_PENSION_720,
    BUY_LOTTO_645_SERVICE_NAME,
    BUY_PENSION_720_SERVICE_NAME,
    REFRESH_LOTTERY_SERVICE_NAME,
)
from .coordinator import DhLotto645Coordinator, DhLotteryCoordinator, DhPension720Coordinator

_LOGGER = logging.getLogger(__name__)

type DhLotteryConfigEntry = ConfigEntry[DhLotteryData]  # noqa: F821


@dataclass
class DhLotteryData:
    """DH Lottery data class."""

    lottery_coord: DhLotteryCoordinator = None
    lotto_645_coord: Optional[DhLotto645Coordinator] = None
    pension_720_coord: Optional[DhPension720Coordinator] = None
    entry: Optional[ConfigEntry] = None


BUY_LOTTO_645_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
        vol.Required("game_1"): cv.string,
        vol.Optional("game_2"): cv.string,
        vol.Optional("game_3"): cv.string,
        vol.Optional("game_4"): cv.string,
        vol.Optional("game_5"): cv.string,
    }
)


BUY_PENSION_720_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
        vol.Optional("mode", default="manual"): vol.In(["manual", "auto"]),
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: DhLotteryConfigEntry) -> bool:
    """설정 항목을 설정합니다."""
    hass.data.setdefault(DOMAIN, {})
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    client = DhLotteryClient(username, password)
    try:
        await client.async_login()
    except DhLotteryError as ex:
        raise ConfigEntryNotReady(f"동행 복권 로그인 실패: {ex}") from ex

    data = DhLotteryData(DhLotteryCoordinator(hass, client), entry=entry)

    # 센서 플랫폼 설정 전에 coordinator 첫 새로고침 수행
    try:
        await data.lottery_coord.async_config_entry_first_refresh()
    except Exception as ex:
        raise ConfigEntryNotReady(f"예치금 정보 조회 실패: {ex}") from ex

    if entry.data[CONF_LOTTO_645]:
        data.lotto_645_coord = DhLotto645Coordinator(
            hass, client, data.lottery_coord.async_clear_refresh
        )
        try:
            await data.lotto_645_coord.async_config_entry_first_refresh()
        except Exception as ex:
            raise ConfigEntryNotReady(f"로또 정보 조회 실패: {ex}") from ex

    if entry.data.get(CONF_PENSION_720, False):
        data.pension_720_coord = DhPension720Coordinator(
            hass, client, entry, data.lottery_coord.async_clear_refresh
        )
        try:
            await data.pension_720_coord.async_config_entry_first_refresh()
        except Exception as ex:
            raise ConfigEntryNotReady(f"연금복권 정보 조회 실패: {ex}") from ex

    entry.runtime_data = data
    hass.data[DOMAIN][entry.entry_id] = data

    await _async_setup_service(hass, entry)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DhLotteryConfigEntry) -> bool:
    """설정 항목을 언로드합니다."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """업데이트 리스너"""
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_setup_service(
    hass: HomeAssistant, entry: DhLotteryConfigEntry
) -> None:
    """서비스를 설정합니다."""

    async def _async_lottery_refresh(call: ServiceCall) -> None:
        """로또 정보를 새로고침합니다."""
        for lottery_data in hass.data[DOMAIN].values():
            await lottery_data.lottery_coord.async_clear_refresh()
            if lottery_data.lotto_645_coord:
                await lottery_data.lotto_645_coord.async_clear_refresh()
            if lottery_data.pension_720_coord:
                await lottery_data.pension_720_coord.async_clear_refresh()

    def _find_lottery_data(deposit_id: str) -> DhLotteryData:
        registry = er.async_get(hass)
        registry_entry = registry.async_get(deposit_id)
        if not registry_entry:
            raise ValueError(f"예치금 엔티티 '{deposit_id}'를 찾을 수 없습니다.")
        if registry_entry.config_entry_id not in hass.data[DOMAIN]:
            raise ValueError(f"예치금 엔티티 '{deposit_id}'를 찾을 수 없습니다.")
        return hass.data[DOMAIN][registry_entry.config_entry_id]

    async def _async_buy_lotto_645(call: ServiceCall) -> ServiceResponse:
        """로또 6/45를 구매합니다."""
        lottery_data: DhLotteryData | None = None
        try:
            lottery_data = _find_lottery_data(call.data["entity_id"])
            items: List[DhLotto645.Slot] = []
            for i in range(1, 6):
                if f"game_{i}" in call.data:
                    texts = call.data[f"game_{i}"].strip().split(",")
                    sel_mode = DhLotto645SelMode(texts[0])
                    if sel_mode == DhLotto645SelMode.AUTO:
                        items.append(DhLotto645.Slot(DhLotto645SelMode.AUTO))
                    else:
                        items.append(
                            DhLotto645.Slot(sel_mode, [int(text) for text in texts[1:]])
                        )
            result = await lottery_data.lotto_645_coord.lotto_645.async_buy(items)
            number_text = "\n".join(
                [
                    f"- {game.slot} {game.mode} {' '.join(map(str, game.numbers))}"
                    for game in result.games
                ]
            )
            message = f"제 {result.round_no}회\n발행일: {result.issue_dt}\n바코드: {result.barcode}\n번호:\n{number_text}"
            persistent_notification.async_create(
                hass, message, "로또 6/45 구매", call.context.id
            )
            
            # Discord Webhook Notification
            webhook_url = lottery_data.entry.data.get("discord_webhook_url") if lottery_data and lottery_data.entry else None
            if webhook_url:
                discord_msg = (
                    f"📢 **동행복권 로또 6/45 구매 완료**\n"
                    f"- **회차**: 제 {result.round_no}회\n"
                    f"- **발행일**: {result.issue_dt}\n"
                    f"- **바코드**: {result.barcode}\n"
                    f"- **구매 번호**:\n{number_text}"
                )
                hass.async_create_task(lottery_data.lottery_coord.client.async_send_to_discord(webhook_url, discord_msg))

            return {
                "result": "success",
                "value": result.to_dict(),
            }
        except Exception as e:
            persistent_notification.async_create(
                hass, str(e), "로또 6/45 구매 실패", call.context.id
            )
            
            # Discord Webhook Notification
            webhook_url = lottery_data.entry.data.get("discord_webhook_url") if lottery_data and lottery_data.entry else None
            if webhook_url:
                discord_msg = f"❌ **동행복권 로또 6/45 구매 실패**\n- **사유**: {str(e)}"
                hass.async_create_task(lottery_data.lottery_coord.client.async_send_to_discord(webhook_url, discord_msg))

            return {
                "result": "fail",
                "message": str(e),
            }
        finally:
            if lottery_data:
                await lottery_data.lottery_coord.async_clear_refresh()
                if lottery_data.lotto_645_coord:
                    await lottery_data.lotto_645_coord.async_clear_refresh()

    async def _async_buy_pension_720(call: ServiceCall) -> ServiceResponse:
        """연금복권 720+를 구매합니다."""
        lottery_data: DhLotteryData | None = None
        try:
            lottery_data = _find_lottery_data(call.data["entity_id"])
            if not lottery_data.pension_720_coord:
                raise ValueError("연금복권 코디네이터가 활성화되어 있지 않습니다.")
            mode = call.data.get("mode", "manual")
            if mode == "auto":
                candidates = []
            else:
                entry = lottery_data.entry
                raw_nums = entry.options.get("pension_manual_numbers", entry.data.get("pension_manual_numbers", "810212,810410,120911,150402"))
                candidates = [x.strip() for x in raw_nums.split(",") if x.strip().isdigit() and len(x.strip()) == 6]
                if not candidates:
                    candidates = ["810212", "810410", "120911", "150402"]

            result = await lottery_data.pension_720_coord.pension_720.async_buy(candidates=candidates)
            number_text = "\n".join(
                [
                     f"- {game.group} {game.numbers} ({game.status})"
                     for game in result.games
                ]
            )
            message = f"제 {result.round_no}회\n발행일: {result.issue_dt}\n주문번호: {result.order_no}\n번호:\n{number_text}"
            persistent_notification.async_create(
                hass, message, "연금복권 720+ 구매", call.context.id
            )
            
            # Discord Webhook Notification
            webhook_url = None
            if lottery_data and lottery_data.entry:
                webhook_url = lottery_data.entry.options.get("discord_webhook_url", lottery_data.entry.data.get("discord_webhook_url"))
            if webhook_url:
                failed_candidates_str = ", ".join(result.failed_candidates) if getattr(result, 'failed_candidates', None) else "없음"
                success_candidate_str = result.success_candidate if getattr(result, 'success_candidate', None) else "없음"
                
                discord_msg = (
                    f"📢 **동행복권 연금복권 720+ 구매 완료**\n"
                    f"- **회차**: 제 {result.round_no}회\n"
                    f"- **구매일시**: {result.issue_dt}\n"
                    f"- **주문번호**: {result.order_no}\n"
                    f"- **성공번호**: {success_candidate_str}\n"
                    f"- **실패번호**: {failed_candidates_str}\n"
                    f"- **구매 번호**:\n{number_text}"
                )
                hass.async_create_task(lottery_data.lottery_coord.client.async_send_to_discord(webhook_url, discord_msg))

            return {
                "result": "success",
                "value": result.to_dict(),
            }
        except Exception as e:
            persistent_notification.async_create(
                hass, str(e), "연금복권 720+ 구매 실패", call.context.id
            )
            
            # Discord Webhook Notification
            webhook_url = None
            if lottery_data and lottery_data.entry:
                webhook_url = lottery_data.entry.options.get("discord_webhook_url", lottery_data.entry.data.get("discord_webhook_url"))
            if webhook_url:
                discord_msg = f"❌ **동행복권 연금복권 720+ 구매 실패**\n- **사유**: {str(e)}"
                hass.async_create_task(lottery_data.lottery_coord.client.async_send_to_discord(webhook_url, discord_msg))

            return {
                "result": "fail",
                "message": str(e),
            }
        finally:
            if lottery_data:
                await lottery_data.lottery_coord.async_clear_refresh()
                if lottery_data.pension_720_coord:
                    await lottery_data.pension_720_coord.async_clear_refresh()

    hass.services.async_register(
        DOMAIN,
        REFRESH_LOTTERY_SERVICE_NAME,
        _async_lottery_refresh,
    )
    if entry.data[CONF_LOTTO_645]:
        hass.services.async_register(
            DOMAIN,
            BUY_LOTTO_645_SERVICE_NAME,
            _async_buy_lotto_645,
            schema=BUY_LOTTO_645_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
    if entry.data.get(CONF_PENSION_720, False):
        hass.services.async_register(
            DOMAIN,
            BUY_PENSION_720_SERVICE_NAME,
            _async_buy_pension_720,
            schema=BUY_PENSION_720_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
