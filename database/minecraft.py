import requests

from colorlogx import get_logger
logger = get_logger("minecraftApi")

# Mojang's session server: the current name of an account (Java Edition, online mode). The single source of truth
# for player names (see DatabaseManager.add_player).
PROFILE_URL = "https://sessionserver.mojang.com/session/minecraft/profile/{}"


class Minecraft:
    def __init__(self, timeout=5):
        self.timeout = timeout

    def get_player_name_from_mojang_uuid_online(self, uuid):
        """
        The current name of a Mojang account.

        :param uuid: UUID of the player.
        :return: The username, or None if Mojang does not know the uuid (offline-mode or Bedrock player) or cannot
                 be reached.
        """
        try:
            response = requests.get(PROFILE_URL.format(str(uuid).replace("-", "")), timeout=self.timeout)
            if response.status_code == 200:
                user_name = response.json()["name"]
                logger.debug(f"Found username: {user_name} for uuid: {uuid}")
                return user_name
            if response.status_code not in (204, 404):
                logger.error(f"Failed to retrieve username for uuid: {uuid}. Status code: {response.status_code}")
        except Exception as e:
            logger.error(f"Error in get_player_name_from_mojang_uuid_online: {e}")
        return None
