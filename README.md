# soga-line-bot

LINE 群組登記小幫手。把 bot 拉進牌友群組，有人打「+五晚」，bot 就把他的 SOGA 名字填進這週的表、在群組裡回整張表。兩個群組共用同一張表。flask soga（計分程式）再透過 API 讀這張表，給前端顯示。

```
LINE 群組 ──webhook──▶ https://sogamj.com/api/line/webhook
                         （統計站 Next.js：app/api/line/webhook/route.ts，原封不動轉送）
                              │ soga-network
                              ▼
                       soga-line-bot 容器（NAS）── /data/line_bot.db
                              │   └─ /soga-data/soga.db（統計站那份，唯讀，玩家名單）
                              ▲
     flask soga（計分電腦）──GET http://192.168.1.105:12160/api/signups（X-Api-Key）
          └─ GET /api/line-signups ◀── 前端
```

## 群組裡的指令

只有符合格式的訊息才會回，一般聊天不理。

| 打這個 | 結果 |
| --- | --- |
| `+五晚` | 把自己填進週五晚上。星期 `一`～`日`（`天` 也算），時段 `早 午 晚 夜` |
| `+一晚 三晚` | 一次登記好幾個 |
| `+六晚 晚點到` | 時段後面的字是自己的備註，表上變成「小魚（晚點到）」；已經登記過的再打一次就是改備註（最多 15 字） |
| `-五晚` | 取消 |
| `取消`、`全部取消` | 取消自己今天起所有的登記 |
| `名單`、`表` | 看這週的表（下週已經有人登記就一起列） |
| `綁定 小魚` | 把自己的 LINE 帳號對到 SOGA 名字；`綁定` 單打是查目前綁的是誰 |
| `說明` | 指令說明（bot 剛進群組時也會自動發一次） |

每個時段最多 4 人（`SOGA_SLOT_CAPACITY`），滿了 bot 會回「❌ 五晚 已經滿 4 人了」，有人取消就空出位子。兩群同時搶最後一個位子也只會進去一個。

日期是今天起算最近的那一天：週五打 `+五晚` 是今晚，打 `+四晚` 是下週四。

回覆長這樣（照群組原本那張表）：

```
✅ 小魚 +五晚

26/10/19 - 26/10/25
一晚 日日 吉詳 小魚
...
六午 賤綠 熠熠 玖玖
日午
日晚 小米 鴨鴨 糖果
```

### 固定班底（每週預設的表）

班底表是 NAS 上的一個文字檔，格式跟群組那張表一樣，直接打開改、存檔就生效，不用重新部署：

```
\192.168.1.105\home\soga-line-bot\data\weekly_defaults.txt
```

（NAS 路徑 `/volume1/homes/yc/soga-line-bot/data/weekly_defaults.txt`；repo 裡的 [weekly_defaults.txt](weekly_defaults.txt) 只是第一次部署時放上去的初始版本，之後部署不會覆蓋 NAS 上那份。）

- 管理員也可以直接在 LINE 改：打 `班底` 看目前的表，`修改班底`（或 `修改DEFAULT`）＋換行＋新表 就整份換掉。括號備註不會寫進班底、舊名字自動換成目前的名字，舊的那份備份在 `data/weekly_defaults_history/`。
- 某一週第一次被用到（有人看表或登記那週）時，bot 讀當下的班底表帶入，剩下的位子大家自己 `+`。
- **改了班底表之後，從下一個還沒帶入的週開始用新表**；已經帶入的週（通常是這週，週末有人排下週時下週也會先帶入）照舊，不會被改掉。
- 每一行也決定那週固定列出的時段（沒人也列，例如 `日午`）；其他時段有人登記才出現。
- 班底也算在每格 4 人裡面：`二晚` 班底已經 4 個人，別人就登記不進去。
- 班底某週不能來，自己打 `-一晚` 取消那一週；那週不會再被補回去，下週照樣帶入。`取消` 也會把他今天起的班底位子一起取消。
- 班底不用先綁定也會出現在表上。他第一次綁定（或 LINE 名字自動對上）時，那些位子就歸到他的 LINE 帳號，之後才能自己取消。
- 看不懂的行（格式不是「星期＋時段 名字…」）會略過，並記在 `docker logs soga-line-bot`。
- `名單` 在週六、週日，或下週已經有人登記時，會連下週一起列。

群組裡回的表只列**今天以後**的時段（今天的都還列著），這週已經過去的那幾天不顯示；查詢 API 則整週都給。

### 直接改某一週的表（管理用）

群組外面已經排好的表要搬進來時，在 NAS 上：

```
docker exec -i soga-line-bot python -m soga_line_bot.set_board < 表.txt
```

格式跟群組那張表一樣，名字後面可以接括號備註（`兔子（1415到）`）。只改有列出來的時段，沒列的不動；`--week 2026-10-26` 改別週，`--dry-run` 只看不寫。詳見 [set_board.py](soga_line_bot/set_board.py)。

### 名字怎麼來

表上是 SOGA 的名字（soga.db 的 `history_player_info.current_name`），不是 LINE 名字：

1. 綁定過 → 用綁定的名字。
2. 沒綁定、但 LINE 名字剛好等於某個 SOGA 名字 → 自動綁上。
3. 都不是 → bot 請他先打 `綁定 名字`。名字不在 SOGA 名單裡也能綁（新朋友），bot 會提醒一下。

綁定記的是 SOGA 的玩家 id（`history_player_info.id`），不是名字：玩家改名後表上自動顯示新名字，不用重綁；用舊名字綁定、或班底表裡寫舊名字，也會透過 `history_name` 對到同一個人。不在 soga.db 的人（新人）沒有 id，以名字綁定，表上標「（新人）」。

一個 SOGA 名字只能被一個 LINE 帳號綁，避免有人冒名。改綁定之後舊的登記也跟著換名字。名單來自 NAS 上統計站那份 soga.db，scheduler-soga 每天同步一次。

### 為什麼不主動推播到另一群

回覆免費，推播要扣額度：免費方案每月 200 則，而且推到群組是**按人數**算。兩群各 30 人、一天 5～10 次變動，即時推播一個月要好幾千則。所以改成兩群共用同一張表，誰在哪一群打指令都會看到最新的整張表。

### 群組權限

- `LINE_ALLOWED_CHAT_IDS`：可以登記的群組（Soga.2026new、SOGA.）。
- `LINE_READONLY_CHAT_IDS`：只能看表的群組（旺粉專區），只回 `名單`，其他指令、進群說明都不回。
- 一對一私訊 bot 不受限制（很多人在私訊裡綁定）。
- `LINE_ADMIN_USER_IDS`：可以 `覆蓋` 整張表的 LINE 帳號。

## 查詢 API

`GET /api/signups?from=2026-10-19&to=2026-11-01`，header 帶 `X-Api-Key`。`from` 預設這週一，`to` 預設 `from + 13 天`。回傳 `signups`（每一筆登記：`date`、`period`、`slot`、`name`、`chatName`…）與 `weeks`（範圍內每一週的表：`text` 是整張表的文字，`slots` 是拆開的資料）。flask soga 的 `GET /api/line-signups` 收一樣的參數、回一樣的內容。

## 第一次上線

1. **開 LINE channel**（要本人在 [LINE Developers](https://developers.line.biz/console/) 做）：建 Provider → 建 Messaging API channel。
   - Basic settings 抄 **Channel secret**；Messaging API 分頁按 Issue 拿 **Channel access token (long-lived)**。
   - Messaging API 分頁：**Allow bot to join group chats** 打開；Webhook URL 填 `https://sogamj.com/api/line/webhook`，**Use webhook** 打開。
   - 在 LINE Official Account Manager 把「自動回應訊息」關掉，不然 bot 會對每句話回罐頭訊息。
2. **設定**：`deploy/env.production.example` → `deploy/.env.production` 填好；`deploy/deploy_synology.env.example` → `deploy/.deploy_synology.env`（可以直接複製 soga_live 那份，改 `SYNO_BASE_DIR`）。
3. **部署 bot**：`python deploy/deploy.py`（走 `nas_safe_build.py`，不會直接 `docker-compose build`）。
4. **部署公開站**：統計站要重新部署一次，`/api/line/webhook` 這條代理才會上線（`SogaStatisticDashboardBackend/deploy/deploy.py`，已經加好 `LINE_BOT_URL`）。
5. 回 LINE Developers 按 Webhook URL 旁的 **Verify**，要回 Success。
6. 掃 QR code 加 bot 好友，拉進群組。從 `docker logs soga-line-bot` 抄群組的 `chat=C...`，填進 `LINE_ALLOWED_CHAT_IDS` 再部署一次，bot 就只理這個群組。
7. **flask soga**：`flask_soga_web/.env` 加 `SOGA_LINE_BOT_API_KEY`（跟 bot 同一把），重啟後 `http://localhost:2140/api/line-signups` 就有資料。

## 本機開發

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

登記資料只在 NAS 的 `/volume1/homes/yc/soga-line-bot/data/line_bot.db`，取消不刪資料只標 `cancelled_at`。
