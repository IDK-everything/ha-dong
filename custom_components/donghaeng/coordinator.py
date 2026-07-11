import datetime
import logging
from dataclasses import dataclass
from typing import Any, Optional, List, Callable, Awaitable

import async_timeout

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from .client.dh_lottery_client import (
    DhLotteryError,
    DhLotteryClient,
    DhLotteryBalanceData,
)
from .client.dh_lotto_645 import DhLotto645
from .client.dh_pension_720 import DhPension720, DhPension720Game, DhPension720BuyHistoryData
from .const import (
    COORDINATOR_UPDATE_INTERVAL,
    LOTTO_645_UPDATE_INTERVAL,
    LOTTERY_ACCUMULATED_PRIZE_UPDATE_INTERVAL,
    LOTTERY_BALANCE_UPDATE_INTERVAL,
    PENSION_720_UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(order=True)
class DhLotto645BuyData:
    """로또 구매 내역을 나타내는 데이터 클래스입니다."""

    round_no: int
    barcode: str
    game: DhLotto645.Game
    result: str
    rank: int = None


class DhCoordinator(DataUpdateCoordinator):
    """동행복권 데이터 업데이트 코디네이터입니다."""

    client: DhLotteryClient


class DhLotteryCoordinator(DhCoordinator):
    """동행복권 데이터 업데이트 코디네이터입니다."""

    def __init__(self, hass: HomeAssistant, client: DhLotteryClient):
        super().__init__(
            hass,
            _LOGGER,
            name="DhLotteryCoordinator",
            update_interval=COORDINATOR_UPDATE_INTERVAL,
        )
        self.client = client
        self._balance_last_updated: Optional[datetime.datetime] = None
        self._accumulated_prize_last_updated: Optional[datetime.datetime] = None

    async def _async_update_data(self) -> dict[str, Any]:
        """동행복권 데이터를 비동기로 업데이트합니다."""
        now = datetime.datetime.now()

        # 새벽 0시 ~ 6시 (판매 중지 시간) 접속 차단 로직
        if 0 <= now.hour < 6 and self.data is not None:
            return self.data
        
        try:
            balance: Optional[DhLotteryBalanceData] = None
            if self._check_update_balance(now):
                async with async_timeout.timeout(10):
                    _LOGGER.info("예치금 정보를 업데이트합니다.")
                    balance = await self.client.async_get_balance()
                    self._balance_last_updated = now

            accumulated_prize: Optional[int] = None
            if self._check_update_accumulated_prize(now):
                async with async_timeout.timeout(10):
                    _LOGGER.info("누적 당첨금을 업데이트 합니다.")
                    accumulated_prize = await self.client.async_get_accumulated_prize("LO40")
                    self._accumulated_prize_last_updated = now

            return {
                "balance": balance,
                "accumulated_prize": accumulated_prize,
                "update_dt": now.strftime("%Y-%m-%d %H:%M:%S"),
            }
        # except DhLotteryLoginError as err:
        # Raising ConfigEntryAuthFailed will cancel future updates
        # and start a config flow with SOURCE_REAUTH (async_step_reauth)
        # raise ConfigEntryAuthFailed from err
        except DhLotteryError as err:
            raise UpdateFailed(f"API와의 통신 오류: {err}")

    async def async_clear_refresh(self):
        """데이터를 새로고침합니다."""
        self._balance_last_updated = None
        self._accumulated_prize_last_updated = None
        await self.async_request_refresh()

    def _check_update_balance(self, now: datetime.datetime) -> bool:
        """예치금 정보를 업데이트할지 확인합니다."""
        if not self._balance_last_updated:
            return True
        return (now - self._balance_last_updated) >= LOTTERY_BALANCE_UPDATE_INTERVAL

    def _check_update_accumulated_prize(self, now: datetime.datetime) -> bool:
        """누적 당첨금을 업데이트할지 확인합니다."""
        if not self._accumulated_prize_last_updated:
            return True
        return (
            now - self._accumulated_prize_last_updated
        ) >= LOTTERY_ACCUMULATED_PRIZE_UPDATE_INTERVAL


class DhLotto645Coordinator(DhCoordinator):
    """로또 6/45 데이터 업데이트 코디네이터입니다."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: DhLotteryClient,
        lottery_refresh_func: Callable[[], Awaitable[None]],
    ):
        super().__init__(
            hass,
            _LOGGER,
            name="DhLotto645Coordinator",
            update_interval=COORDINATOR_UPDATE_INTERVAL,
        )
        self.client = client
        self.lotto_645 = DhLotto645(client)
        self.lottery_refresh_func = lottery_refresh_func
        self._latest_winning_numbers: Optional[DhLotto645.WinningData] = None
        self._buy_history_last_updated: Optional[datetime.datetime] = None
        self.winning_dict: dict[int, DhLotto645.WinningData] = {}

    async def _async_update_data(self) -> dict[str, Any]:
        """Lotto 6/45 데이터를 비동기로 업데이트합니다."""
        now = datetime.datetime.now()

        # 새벽 0시 ~ 6시 (판매 중지 시간) 접속 차단 로직
        if 0 <= now.hour < 6 and self.data is not None:
            return self.data
        
        try:
            latest_winning_numbers: Optional[DhLotto645.WinningData] = None

            if self._check_update_winning_numbers(now):
                async with async_timeout.timeout(10):
                    _LOGGER.info("당첨 번호를 업데이트합니다.")
                    latest_round_no = await self.lotto_645.async_get_latest_round_no()
                    latest_winning_numbers = await self._async_get_winning_numbers(
                        latest_round_no
                    )
                    self._latest_winning_numbers = latest_winning_numbers
                    # 최신 회차를 업데이트 할 때, 구매 내역, 예치금, 누적 당첨금이 같이 업데이트 되도록 함
                    if self._buy_history_last_updated:
                        await self.lottery_refresh_func()
                    self._buy_history_last_updated = None

            buy_history_this_week: List[DhLotto645BuyData] = []
            if self._async_check_update_buy_history(now):
                async with async_timeout.timeout(10):
                    _LOGGER.info("이번 주의 구매 내역을 업데이트합니다.")
                    buy_history_this_week = (
                        await self._async_get_buy_history_this_week()
                    )
                    self._buy_history_last_updated = now

            return {
                "latest_winning_numbers": latest_winning_numbers,
                "buy_history_this_week": buy_history_this_week,
                "update_dt": now.strftime("%Y-%m-%d %H:%M:%S"),
            }
        # except DhLotteryLoginError as err:
        # Raising ConfigEntryAuthFailed will cancel future updates
        # and start a config flow with SOURCE_REAUTH (async_step_reauth)
        # raise ConfigEntryAuthFailed from err
        except DhLotteryError as err:
            raise UpdateFailed(f"API와의 통신 오류: {err}") from err

    async def async_clear_refresh(self):
        """데이터를 새로고침합니다."""
        self._latest_winning_numbers = None
        self._buy_history_last_updated = None
        self.winning_dict = {}
        await self.async_request_refresh()

    def _check_update_winning_numbers(self, now: datetime.datetime) -> bool:
        """당첨 번호를 업데이트할지 확인합니다."""
        if not self._latest_winning_numbers:
            return True
        # 현재 시각이 토요일 20:40 ~ 21:30 사이인지 확인합니다.
        if now.weekday() == 5 and datetime.time(20, 40) <= now.time() <= datetime.time(
            21, 30
        ):
            if now.strftime("%Y-%m-%d") != self._latest_winning_numbers.draw_date:
                return True
        return False

    def _async_check_update_buy_history(self, now: datetime.datetime) -> bool:
        """구매 내역을 업데이트할지 확인합니다."""
        if not self._buy_history_last_updated:
            return True
        return (now - self._buy_history_last_updated) >= LOTTO_645_UPDATE_INTERVAL

    async def _async_get_buy_history_this_week(self) -> List[DhLotto645BuyData]:
        """이번 주의 구매 내역을 가져옵니다."""

        def calculate_rank(
            my_numbers: List[int], win_numbers: List[int], bonus: int
        ) -> int:
            """로또 등수를 계산합니다."""
            same_cnt = 0  # 일치하는 개수

            for num in win_numbers:  # 각 당첨 번호 포함 여부 체크
                if num in my_numbers:
                    same_cnt += 1
            # 등수 반환
            if same_cnt == 6:
                return 1
            if same_cnt == 5 and bonus in my_numbers:
                return 2
            if same_cnt == 5:
                return 3
            if same_cnt == 4:
                return 4
            if same_cnt == 3:
                return 5
            else:
                return 0  # 꽝

        async def async_get_rank(_result: str, _numbers: List[int]) -> int:
            """등수를 비동기로 가져옵니다."""
            if _result == "미추첨":
                return -1
            if "당첨" in _result:
                winning_numbers = await self._async_get_winning_numbers(item.round_no)
                return calculate_rank(
                    _numbers, winning_numbers.numbers, winning_numbers.bonus_num
                )
            return 0

        items: List[DhLotto645BuyData] = []
        for item in await self.lotto_645.async_get_buy_history_this_week():
            for game in item.games:
                items.append(
                    DhLotto645BuyData(
                        round_no=item.round_no,
                        barcode=item.barcode,
                        game=game,
                        rank=await async_get_rank(item.result, game.numbers),
                        result=item.result,
                    )
                )
                if len(items) >= 5:
                    break
        return items

    async def _async_get_winning_numbers(self, round_no: int):
        """당첨 번호를 비동기로 가져옵니다."""
        winning_data = self.winning_dict.get(round_no)
        if not winning_data:
            winning_data = await self.lotto_645.async_get_round_info(round_no)
            self.winning_dict[round_no] = winning_data
        return winning_data


class DhPension720Coordinator(DhCoordinator):
    """연금복권 720+ 데이터 업데이트 코디네이터입니다."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: DhLotteryClient,
        entry: Any,
        lottery_refresh_func: Callable[[], Awaitable[None]],
    ):
        super().__init__(
            hass,
            _LOGGER,
            name="DhPension720Coordinator",
            update_interval=COORDINATOR_UPDATE_INTERVAL,
        )
        self.client = client
        self.entry = entry
        self.pension_720 = DhPension720(client)
        self.lottery_refresh_func = lottery_refresh_func
        self._latest_round_no: Optional[int] = None
        self._buy_history_last_updated: Optional[datetime.datetime] = None
        self._last_pension_buy_attempt_time: Optional[datetime.datetime] = None
        self._last_pension_buy_attempt_round: Optional[int] = None

    async def _async_update_data(self) -> dict[str, Any]:
        """연금복권 720+ 데이터를 비동기로 업데이트합니다."""
        now = datetime.datetime.now()

        # 새벽 0시 ~ 6시 (판매 중지 시간) 접속 차단 로직
        if 0 <= now.hour < 6 and self.data is not None:
            return self.data
        
        try:
            # 1. 최신 회차 번호 갱신
            if self._check_update_round_no(now) or self._latest_round_no is None:
                async with async_timeout.timeout(10):
                    _LOGGER.info("연금복권 최신 회차 번호를 업데이트합니다.")
                    latest_round_no = await self.pension_720.async_get_latest_round_no()
                    self._latest_round_no = latest_round_no
                    if self._buy_history_last_updated:
                        await self.lottery_refresh_func()
                    self._buy_history_last_updated = None

            # 2. 이번 주 구매 내역 갱신
            buy_history_this_week: List[DhPension720BuyHistoryData] = []
            if self._async_check_update_buy_history(now) or self.data is None:
                async with async_timeout.timeout(10):
                    _LOGGER.info("이번 주의 연금복권 구매 내역을 업데이트합니다.")
                    buy_history_this_week = (
                        await self.pension_720.async_get_buy_history_this_week()
                    )
                    self._buy_history_last_updated = now
            else:
                buy_history_this_week = self.data.get("buy_history_this_week", [])

            # 3. 설정된 요일/시간에 따른 백그라운드 자동 구매 스케줄러 실행
            buy_time_str = self.entry.options.get("pension_buy_time", self.entry.data.get("pension_buy_time", "17:00"))
            buy_weekday_str = self.entry.options.get("pension_buy_weekday", self.entry.data.get("pension_buy_weekday", "목요일"))

            weekday_map = {"월요일": 0, "화요일": 1, "수요일": 2, "목요일": 3, "금요일": 4, "토요일": 5, "일요일": 6}
            configured_weekday = weekday_map.get(buy_weekday_str, 3)

            is_buy_time = False
            if now.weekday() == configured_weekday:
                try:
                    buy_hour, buy_minute = map(int, buy_time_str.split(":"))
                    # 설정 시각부터 정확히 5분 이내에만 구매 시도 (이후 재시도 방지)
                    configured_time = datetime.time(buy_hour, buy_minute)
                    now_time = now.time()
                    now_minutes = now_time.hour * 60 + now_time.minute
                    cfg_minutes = configured_time.hour * 60 + configured_time.minute
                    if 0 <= (now_minutes - cfg_minutes) < 5:
                        is_buy_time = True
                except Exception as ex:
                    _LOGGER.error(f"구매 시간 파싱 실패 (값: {buy_time_str}): {ex}")

            if is_buy_time and self._latest_round_no is not None:
                target_round = self._latest_round_no + 1
                already_bought = any(history.round_no == target_round for history in buy_history_this_week)
                
                # Cooldown check: Do not retry if we attempted in the last 30 minutes for this target round
                cooldown_active = (
                    self._last_pension_buy_attempt_round == target_round
                    and self._last_pension_buy_attempt_time is not None
                    and (now - self._last_pension_buy_attempt_time) < datetime.timedelta(minutes=30)
                )

                # 새벽 0시~6시 등 판매정지 시간이 아닐 때만 구매
                is_valid_sales_time = True
                if now.weekday() == 3 and 17 <= now.hour < 20: # 목요일 17~20시 판매 정지
                    is_valid_sales_time = False
                if 0 <= now.hour < 6: # 매일 0~6시 판매 정지
                    is_valid_sales_time = False

                if not already_bought and is_valid_sales_time and not cooldown_active:
                    _LOGGER.info(f"설정된 자동 구매 시간 도달. 연금복권 {target_round}회 자동 구매를 진행합니다.")
                    self._last_pension_buy_attempt_round = target_round
                    self._last_pension_buy_attempt_time = now
                    try:
                        raw_nums = self.entry.options.get("pension_manual_numbers", self.entry.data.get("pension_manual_numbers", "810212,810410,120911,150402"))
                        candidates = [x.strip() for x in raw_nums.split(",") if x.strip().isdigit() and len(x.strip()) == 6]
                        if not candidates:
                            candidates = ["810212", "810410", "120911", "150402"]

                        result = await self.pension_720.async_buy(candidates=candidates, allow_auto_fallback=True)
                        # 구매 후 내역 캐시를 즉시 무효화하여 already_bought가 다음 체크에서 정확히 반영되도록 합니다.
                        self._buy_history_last_updated = None
                        await self.lottery_refresh_func()
                        buy_history_this_week = await self.pension_720.async_get_buy_history_this_week()
                        
                        # Discord Webhook Notification
                        webhook_url = self.entry.options.get("discord_webhook_url", self.entry.data.get("discord_webhook_url")) if self.entry else None
                        if webhook_url:
                            number_text = "\n".join(
                                [
                                     f"- {game.group} {game.numbers} ({game.status})"
                                     for game in result.games
                                ]
                            )
                            failed_candidates_str = ", ".join(result.failed_candidates) if getattr(result, 'failed_candidates', None) else "없음"
                            success_candidate_str = result.success_candidate if getattr(result, 'success_candidate', None) else "없음"
                            remaining_balance = getattr(result, 'remaining_balance', 0)
                            
                            discord_msg = (
                                f"📢 **[자동] 동행복권 연금복권 720+ 구매 완료**\n"
                                f"- **회차**: 제 {result.round_no}회\n"
                                f"- **구매일시**: {result.issue_dt}\n"
                                f"- **주문번호**: {result.order_no}\n"
                                f"- **성공번호**: {success_candidate_str}\n"
                                f"- **실패번호**: {failed_candidates_str}\n"
                                f"- **잔여 예치금**: {remaining_balance:,}원\n"
                                f"- **구매 번호**:\n{number_text}"
                            )
                            self.hass.async_create_task(self.client.async_send_to_discord(webhook_url, discord_msg))
                    except Exception as ex:
                        _LOGGER.error(f"백그라운드 연금복권 자동 구매 중 오류 발생: {ex}")
                        
                        # Discord Webhook Notification
                        webhook_url = self.entry.options.get("discord_webhook_url", self.entry.data.get("discord_webhook_url")) if self.entry else None
                        if webhook_url:
                            discord_msg = f"❌ **[자동] 동행복권 연금복권 720+ 구매 실패**\n- **사유**: {str(ex)}"
                            self.hass.async_create_task(self.client.async_send_to_discord(webhook_url, discord_msg))

            return {
                "latest_round_no": self._latest_round_no,
                "buy_history_this_week": buy_history_this_week,
                "update_dt": now.strftime("%Y-%m-%d %H:%M:%S"),
            }
        except DhLotteryError as err:
            raise UpdateFailed(f"API와의 통신 오류: {err}") from err

    async def async_clear_refresh(self):
        """데이터를 새로고침합니다."""
        self._latest_round_no = None
        self._buy_history_last_updated = None
        await self.async_request_refresh()

    def _check_update_round_no(self, now: datetime.datetime) -> bool:
        """최신 회차를 업데이트할지 확인합니다."""
        if not self._latest_round_no:
            return True
        if now.weekday() == 3 and datetime.time(19, 0) <= now.time() <= datetime.time(20, 0):
            return True
        return False

    def _async_check_update_buy_history(self, now: datetime.datetime) -> bool:
        """구매 내역을 업데이트할지 확인합니다."""
        if not self._buy_history_last_updated:
            return True
        return (now - self._buy_history_last_updated) >= PENSION_720_UPDATE_INTERVAL

