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
// PACK=accept|decline: answer the resource pack offer (the plugin's icons, 3.17). Answered by hand: mineflayer 4
// sends the pack id 00000000-... back, which the plugin (rightly) does not take for its pack.
bot._client.on("add_resource_pack", (data) => {
  console.log(`[${name}] PACK offered: ${data.url}`);
  const answer = { accept: [3, 0], decline: [1] }[process.env.PACK] || [];
  for (const result of answer) bot._client.write("resource_pack_receive", { uuid: data.uuid, result });
});
if (process.env.RAW) {
  bot._client.on("scoreboard_score", (data) => {
    if (data.scoreName === "mccbadge" || data.objectiveName === "mccbadge" || data.objective_name === "mccbadge") {
      console.log(`[${name}] BADGE ${JSON.stringify(data)}`);
    }
  });
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
    if (command.startsWith("dig:")) {  // break up to 4 blocks next to the feet (block stats)
      const offsets = [[1, -1, 0], [-1, -1, 0], [0, -1, 1], [0, -1, -1]].slice(0, Number(command.slice(4)));
      for (const [x, y, z] of offsets) {
        const block = bot.blockAt(bot.entity.position.offset(x, y, z));
        try {
          await bot.dig(block, true);
          console.log(`[${name}] DUG ${block.name}`);
        } catch (e) {
          console.log(`[${name}] DIG FAILED ${block && block.name}: ${e.message}`);
        }
      }
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
