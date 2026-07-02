"""동행 복권 통합 모듈의 상수"""

from datetime import timedelta

from homeassistant.const import Platform
from homeassistant.helpers.device_registry import DeviceInfo

DOMAIN = "dh_lottery"
REFRESH_LOTTERY_SERVICE_NAME = "refresh_lottery"
BUY_LOTTO_645_SERVICE_NAME = "buy_lotto_645"
BUY_PENSION_720_SERVICE_NAME = "buy_pension_720"

TITLE = "동행복권"
TITLE_LOTTO = "로또 6/45"
TITLE_PENSION = "연금복권 720+"
DH_LOTTERY = "DH Lottery"
DH_LOTTO_645 = "DH Lotto 6/45"
DH_PENSION_720 = "DH Pension 720"

BRAND_NAME = "dh"

CONF_LOTTO_645 = "lotto_645"
CONF_PENSION_720 = "pension_720"

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON]

COORDINATOR_UPDATE_INTERVAL = timedelta(minutes=3)
LOTTERY_BALANCE_UPDATE_INTERVAL = timedelta(minutes=60)
LOTTERY_ACCUMULATED_PRIZE_UPDATE_INTERVAL = timedelta(minutes=60)
LOTTO_645_UPDATE_INTERVAL = timedelta(minutes=60)
PENSION_720_UPDATE_INTERVAL = timedelta(minutes=60)


def get_dh_lottery_device_info(username: str) -> DeviceInfo:
    """동행 복권 엔티티에 대한 디바이스 정보를 반환합니다."""
    return DeviceInfo(
        configuration_url="https://dhlottery.co.kr",
        identifiers={(DOMAIN, username)},
        manufacturer=TITLE,
        model=DH_LOTTERY,
        name=TITLE,
    )


def get_dh_lotto_645_device_info(username: str) -> DeviceInfo:
    """Lotto 6/45 엔티티에 대한 디바이스 정보를 반환합니다."""
    return DeviceInfo(
        configuration_url="https://dhlottery.co.kr",
        identifiers={(DOMAIN, f"{username}_{CONF_LOTTO_645}")},
        manufacturer=TITLE,
        model=DH_LOTTO_645,
        name=TITLE_LOTTO,
    )


def get_dh_pension_720_device_info(username: str) -> DeviceInfo:
    """Pension 720+ 엔티티에 대한 디바이스 정보를 반환합니다."""
    return DeviceInfo(
        configuration_url="https://dhlottery.co.kr",
        identifiers={(DOMAIN, f"{username}_{CONF_PENSION_720}")},
        manufacturer=TITLE,
        model=DH_PENSION_720,
        name=TITLE_PENSION,
    )
