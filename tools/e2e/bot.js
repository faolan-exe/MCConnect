// Joins the test server as a player, runs the given chat commands and prints what comes back.
const mineflayer = require("mineflayer");
const [name, ...commands] = process.argv.slice(2);
const bot = mineflayer.createBot({ host: "host.docker.internal", port: 25565, username: name, version: "1.21.4" });
// mineflayer cannot parse some player chat packets of Paper 1.21.4; keep the bot running
process.on("uncaughtException", (e) => console.log(`[${name}] (bot parser error: ${e.message})`));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
bot.on("messagestr", (message) => console.log(`[${name}] CHAT: ${message}`));
if (process.env.RAW) {
  bot._client.on("system_chat", (data) => {
    const json = JSON.stringify(data.content ?? data);
    if (/click/i.test(json)) console.log(`[${name}] RAW clickable: ${(json.match(/"(value|command|url)":\{"type":"string","value":"(\/[^"]*|https?:[^"]*)"/g) || []).join(" ")}`);
  });
  for (const packet of ["scoreboard_objective", "scoreboard_score", "scoreboard_display_objective", "teams"]) {
    bot._client.on(packet, (data) => console.log(`[${name}] RAW ${packet}: ${JSON.stringify(data).slice(0, 160)}`));
  }
}
bot.on("kicked", (reason) => console.log(`[${name}] KICKED: ${JSON.stringify(reason)}`));
bot.on("error", (e) => console.log(`[${name}] ERROR: ${e.message}`));
bot.once("spawn", async () => {
  bot.physicsEnabled = false;  // the bot only chats; its movement got it kicked for "invalid movement"
  await sleep(3000);
  for (const command of commands) {
    if (command.startsWith("wait:")) { await sleep(Number(command.slice(5))); continue; }
    if (command.startsWith("jump:")) {  // jump in place for <ms>
      bot.physicsEnabled = true;
      bot.setControlState("jump", true);
      await sleep(Number(command.slice(5)));
      bot.setControlState("jump", false);
      await sleep(500);
      bot.physicsEnabled = false;
      continue;
    }
    if (command === "sidebar") {
      const board = bot.scoreboard.sidebar;
      const title = board ? (typeof board.title === "string" ? board.title : JSON.stringify(board.title)) : "none";
      console.log(`[${name}] SIDEBAR: ${title} | ${board ? board.items.map((i) => `${i.value}:${i.name}`).join(" | ") : ""}`);
      continue;
    }
    console.log(`[${name}] > ${command}`);
    bot.chat(command);
    await sleep(2500);
  }
  bot.quit();
});
