class Zigbee2MQTTException(Exception):
    pass


class ClientNotConnected(Zigbee2MQTTException):
    def __init__(self) -> None:
        super().__init__('MQTT client not connected')


class RequestTimeout(Zigbee2MQTTException):
    pass


class ErrorResponse(Zigbee2MQTTException):
    pass
