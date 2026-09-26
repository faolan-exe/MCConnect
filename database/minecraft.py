import requests

from colorlogx import get_logger
logger = get_logger("minecraftApi")


class Minecraft:
    def __init__(self, timeout=5):
        self.timeout = timeout

    def get_player_name_from_mojang_uuid_online(self, uuid):
        """
        Get the username for a mojang uuid from playerdb.co.

        :param uuid: UUID of the player.
        :return: The username, or None if it could not be resolved.
        """
        url = f"https://playerdb.co/api/player/minecraft/{uuid}"
        try:
            response = requests.get(url, timeout=self.timeout)
            if response.status_code == 200:
                user_name = response.json()['data']['player']['username']
                logger.debug(f"Found username: {user_name} for uuid: {uuid}")
                return user_name
            logger.error(f"Failed to retrieve username for uuid: {uuid}. Status code: {response.status_code}")
        except Exception as e:
            logger.error(f"Error in get_player_name_from_mojang_uuid_online: {e}")
        return None
