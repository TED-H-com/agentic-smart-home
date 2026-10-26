# 安裝 Agentic Smart Home 0.11.1

以下是 0.11.1 工程預覽候選的安裝流程；發布後從 [v0.11.1 發布頁](https://github.com/TED-H-com/agentic-smart-home/releases/tag/v0.11.1) 取得下載工具與媒體。精確來源、同版媒體與公開 App 頻道的驗證結果見 [驗證紀錄](docs/VERIFICATION.md)；候選尚未完成交付時，不能以舊版 ISO 代替。0.10.0 的公開媒體與歷史驗證仍保留。

官方更新來源的正式公鑰與 fingerprint 已由 Ted 於 2026-10-19 核准並釘選，私鑰由發行端保存在 repo 之外。尚未發布以此金鑰簽署的 Controller image、Host bundle、bootstrap 套件或產品媒體。既有 0.11.0 家庭主機需經獨立身份驗證的圖形 bootstrap 配合 **AH-UPDATE** USB，由本人明確批准一次信任與遷移，不能重跑 installer；操作見 [主機更新](docs/HOST_UPDATES.md#existing-0110-household-first-time-bootstrap)。新安裝只有從經獨立身份驗證、帶有效官方 pin 的媒體，才安裝啟用中的不可變 Host 更新執行器；有效且明確設定 `pin: null`、`image_repository: null` 的工程 fixture 則延後安裝啟用中的執行器。公鑰、完整 fingerprint 與批准流程見 [更新說明](docs/UPDATES.md#官方更新來源與-owner-批准)。

## 需要

- Intel／AMD x86-64 電腦、UEFI，開啟 Intel VT-x／AMD-V。建議 **16 GB RAM、128 GB SSD**；硬體檢查以系統實際可用記憶體至少 **7,680 MiB** 與可建立 VM 的 KVM 為準。約五年前的 Dell 仍需確認實際規格，年份本身不是相容證明。
- 安裝時插上 AC 電源，優先用 **Ethernet 網路線接家庭主網路**；沒有網路孔時可使用 Linux 支援的 USB Ethernet。安裝和第一次設定都需要下載元件。
- 一支可以清除的 **32 GB USB**。可直接用這台筆電的現有 Windows／Linux 系統下載並製作 USB，再重新開機安裝；不必另備一台電腦。
- 筆電準備作專用主機；安裝時選中的內部磁碟會清除。先把要保留的資料搬走。
- 手機保留 Smart Life／Tuya Smart、Hue 等原有 App，後續需要你本人登入、掃碼或按配對鍵。
- 一個 AI 帳號，例如 ChatGPT 訂閱或 API 金鑰。

## 下載與製作 USB

在現有系統先備份需要保留的資料，再從發布頁下載 `download-0.11.1.py`。下載資料夾放在內接磁碟，至少預留 **14 GiB** 空間供分段與合併後的 ISO 使用；Windows 使用 NTFS 資料夾，不要直接下載到 FAT32／exFAT USB。需要 Python 3.8 以上，可由 [Python 官方網站](https://www.python.org/downloads/windows/) 安裝。

在下載程式所在資料夾開啟終端。Windows PowerShell：

```powershell
py -3 --version
py -3 .\download-0.11.1.py --directory .\agentic-home-download
```

找不到 `py` 時，先用 `python --version` 確認版本至少為 3.8，再把命令開頭的 `py -3` 換成 `python`；參考 [Python Windows 說明](https://docs.python.org/3/using/windows.html)。macOS／Linux：

```bash
python3 ./download-0.11.1.py --directory ./agentic-home-download
```

工具預設下載 ISO 與 `SHA256SUMS`，驗證分段與完整檔案後才顯示完成；安裝筆電不需要下載 TAR 或 source ZIP。中斷後重跑相同命令，已驗證的完整分段可續用；校驗不符時會停止並保留原檔，依訊息移開有問題的檔案再重跑。

在 Windows 用 [Rufus](https://rufus.ie/) 選取下載資料夾內的 `agentic-home-0.11.1-laptop-amd64.iso`，核對 **32 GB USB** 的型號與容量後寫入；若詢問 ISO／DD 模式，選 **DD Image mode**。這會清除所選 USB。也可用 balenaEtcher 寫入。工具的 Windows 原生執行仍為 `not_run`；平台與下載驗證範圍見 [驗證紀錄](docs/VERIFICATION.md)。

## 安裝

1. USB 插入專用筆電，接 AC 電源與有線網路。Dell 通常在開機時按 **F12** 開啟一次性開機選單，選 USB 的 UEFI 項目；選單依機型不同，參考 [Dell 開機說明](https://www.dell.com/support/manuals/en-us/latitude-15-3540-laptop/latitude_3540_sm/f12-one-time-boot-menu?guid=guid-71da763a-3f2f-4021-8ef7-3e50da009c74&lang=en-us)。
2. 若硬體檢查指出虛擬化未開啟，重新開機按 **F2** 進入 BIOS，依機型啟用 Intel VT-x／AMD-V，再重試；參考 [Dell 虛擬化說明](https://www.dell.com/support/kbdoc/en-us/000195978/how-to-enable-or-disable-hardware-virtualization-on-dell-systems)。
3. 從 USB 開機，選 **Install Agentic Home Linux**。先檢查 CPU 架構、記憶體與 KVM；不符合時會在磁碟設定前停止，顯示需增加記憶體或開啟 BIOS 虛擬化等原因。通過後，在 Ubuntu 安裝器選有線網路、要安裝的內部磁碟和 Linux 帳號。專用筆電可選使用整顆磁碟，務必核對容量與型號，避免選到外接資料碟。沒有預設密碼，也不會預先選磁碟。

   僅在安裝程式看不到內部 SSD，或提示 Intel RST／RAID 衝突時：先備份要保留的資料，於 Dell 開機標誌按 F2，檢查 SATA Operation／SATA/NVMe Operation。若目前為 RAID On／Intel RST，且要將筆電整碟安裝，可依該機型手冊改成 AHCI 或 AHCI/NVMe；儲存後按 F12 再從 USB 開機，確認磁碟型號與容量。切換可能使原 Windows 無法開機或 RAID 資料無法存取；仍需保留它們時，先完成備份與相應遷移。若已是 AHCI、沒有對應選項或仍找不到 SSD，依實際機型查 Dell 支援。[Dell 儲存模式與 USB 開機說明](https://www.dell.com/support/kbdoc/en-us/000131655/how-to-install-ubuntu-linux-on-your-dell-pc)、[Ubuntu RST 說明](https://ubuntu.com/desktop/docs/en/24.04/reference/intel-rst-during-ubuntu-installation/)

4. 移除 USB 並重新開機。第一次開機會下載桌面套件、Firefox 並載入產品映像；請預留至少 30 分鐘，網路或磁碟較慢時可能更久。想看進度：`journalctl -fu agentic-home-firstboot`。仍有進度時讓它繼續，完成前不要反覆重開機。
5. 用剛建立的帳號登入桌面，**Agentic Home** 會自己打開。沒打開時再點一次桌面捷徑。

桌面登入用的是 120 秒、只能用一次的本機票據。其他帳號或遠端瀏覽器用主機金鑰：`sudo agentic-home access-key`。

## 第一次設定

以下是目前 `main` 的未發行設定流程；0.11.0 原版媒體與既有安裝保留各自畫面及原工作進度，尚未包含所有增量。照畫面完成，關掉頁面不會取消已受理的安裝工作，再打開可續查。

1. **接上有線網路**：將 Ethernet 接到家庭路由器。全新安裝若只有一張可用有線網卡且已取得 DHCP 位址，會嘗試家庭接入；其他情況先保留基本 NAT，之後可明確按 **連上家庭網路**。接法與限制見 [家庭網路接入](docs/HA_LAN.md)。
2. **開始設定**：先自動準備固定版本的 Home Assistant 與管家，不需要先登入開發助手或 AI 帳號。
3. **智能管家**：用 ChatGPT 訂閱或 OpenRouter 金鑰登入主要管家。系統會實際問一次，確認能回覆；這可能用到一點額度。
4. **連接家庭**：在 Home Assistant 建立家庭和擁有者帳號，回來按 **授權連接家庭**，再在家庭確認授權。
5. 完成後，Home Assistant 側欄會出現 **智能管家**，日常直接用 Home Assistant。桌面的 Agentic Home 保留設定、家庭網路接入與救援；可從主機的智能管家展開 **接通家庭網路**，接好後繼續探索設備。
6. **按需啟用**：平常使用網頁即可；開發助手及 LINE／Telegram／Discord 等外部通訊可稍後另行設定。

智能管家與各開發助手的登入各自保存，互不共用。舊安裝若仍顯示 AI 帳號或通訊等步驟，沿原工作繼續，不重新建立家庭。

## 開發助手

- **主機**：設定 → 開發助手 → 登入開發助手（Codex 裝置碼或 API 金鑰），描述問題後按 **開始調查** 或 **開始修改並測試**。成果依序是發現、獨立基準測試結果和修改內容；工作執行中可以停止，失敗時可以再試一次。
- **家庭現場**：同一頁按 **啟用家庭現場**。系統會把 App 裝進 Home Assistant 並完成配對，之後另外登入一次。你在 Home Assistant 停止或移除它，系統不會自動裝回。

開發助手只修改自己的私有副本，沒有家庭權限，成果不會自動部署。

## 第一次實測順序

這是工程預覽版。媒體安裝與隔離平台驗證由開發端完成；真帳號、這台筆電與實體設備由現場驗收。先完成一個可核對的流程，再擴大到全屋：

1. 確認筆電的有線網卡或 USB Ethernet 已接上家庭主網路，完成上方首次設定。使用 Owner 的真帳號登入 Hermes 智能管家並取得實際回覆；保存帳號或顯示登入畫面不算通過。確認 Home Assistant 的 **智能管家** 側欄能打開同一段對話。
2. 登入 **主機** 開發助手，要求一個小修改，例如「只改善 README 的安裝說明，保留行為不變，完成後執行測試」。核對發現、獨立基準測試結果和實際差異；「未執行」不算通過。
3. 按 **啟用家庭現場**，等安裝和配對完成，再登入它自己的 Codex。首次需要從公開 `#stable` 下載並建置 App，可能花數分鐘；頁面顯示安裝或等待時讓原流程繼續。先做一次 **開始調查**，確認成果的觀測位置為 `haos_app`。
4. 只選一盞低風險的實體燈，例如既有 Hue 或 Tuya／Smart Life 燈。在 **設備 → 探索更多設備** 核對來源與時間，按 **追蹤接入**，在 HA 原生頁面完成配對／帳號授權，再選擇整合並讀取結果。核對 HA 讀回的狀態與眼前燈具的實際開關動作一致；管家代做寫入仍需看過提案並批准。探索筆數與 API 成功不代替實體讀回，也不代表 Google Home／Alexa 的全部帳號設備已匯入。
5. 等工作結束並由系統確認停止，記下原工作 ID、已登入的角色、你手動停止的項目和對話。保持瀏覽器頁面開啟，正常重開整台筆電；服務啟動時可顯示等待，回來後確認登入、停止狀態、Home Assistant、側欄、設備狀態與對話保留。不需要在等待時重新登入或配對；先按原工作 ID 續查，避免重送同一工作。
6. 確認恢復後的原工作已結束、系統允許新工作，再明確開始**第二個小工作**，核對它有新的工作 ID、成果與測試結果。若顯示「停止尚未確認」，先依下節處理；不要透過刪紀錄或重啟 App 解除工作區保護。
7. 上述流程通過後，再做下方的測試開關自動化；每次部署仍需看過提案並批准。暫停 AI 後，確認已部署的 HA 規則仍可觸發，再逐項擴大設備。
8. 持續觀察 72 小時，記錄斷線、重登、合蓋／散熱與設備失聯恢復。做一份加密離機備份；長時間測試未完成前，保持這些項目為未驗收。

若家庭現場暫時不能工作，記下它的狀態，仍可繼續驗收智能管家與主機開發助手；不要把未完成的家庭現場項目記為通過。公開 `#stable` 必須提供與主機相同的版本，側欄安裝與家庭現場啟用才能完成。本版需將 `#stable` 與 0.11.1 媒體一同驗證；它是可移動頻道，之後仍須核對版本。舊版媒體的新安裝可能停在「商店版本較新」；新主機應使用已發布且驗證的同版媒體。不要解除版本檢查，也不要在已用的主機直接重跑 installer 覆蓋資料。

### 工作顯示「停止尚未確認」

按該工作上的 **確認已停止**，讓系統核對並重試停止原工作。這個按鈕不代表由你保證程序已停止；只有程式確認後才會釋出工作區。系統也會在背景重試。

如果仍未確認，按原工作 ID 續查並保留紀錄；`needs_reconcile`／`stop_unconfirmed` 會保留工作區租約（lease fence），直到 writer 和 verifier 均被證明停止。不要刪除記錄、解除安裝 App 或反覆重啟來繞過。展開 **技術資料** 記下工作、原因代碼、觀測位置與發生時間，再依下方救援步驟產生本機支援檔。一般支援檔不包含這份工作的詳細啟動原因。確認停止後的失敗／中斷工作不會自動重做；需要時再明確開始新工作。

家庭現場在工作尚未確認停止時異常重啟 App，同一次 Home Assistant OS 開機中的核對仍可能無法證明舊程序已停止。此時保留原工作並交由維護者核對；「確認已停止」不是強制解除保護的重設按鈕。

## HA 原生文字通知

完成既有通訊帳號與收件人授權後，可依 [通知安裝步驟](docs/HA_NOTIFICATIONS.md) 下載並安裝 HA 自訂整合，再從原生自動化呼叫 `agentic_home_notifications.send_text`。先選一個自己可核對的聊天室，送一則測試文字；記錄 HA 的請求結果及聊天室實際觀察。`submitted` 代表通道接受提交，不保證收件人已讀；`unknown` 不會自動重送。0.11.1 媒體包含支援的 Controller 與整合 ZIP，HA 整合仍需依文件安裝。

## 第一條家庭自動化

1. 在 Home Assistant 建立兩個測試用開關：`input_boolean.agentic_motion_test`、`input_boolean.agentic_lamp_test`。
2. 到 **設備** 允許讀取這兩個開關，用途選「測試用開關」，儲存。
3. 對智能管家說：「motion_test 從關閉變開啟時，打開 lamp_test，先讓我看規則。」
4. 看過提案再批准。系統會備份、寫入、啟用，再確認狀態。切換 motion_test，看 lamp_test 是否跟著開啟。

規則由 Home Assistant 執行；停止 AI 不會停止已部署的規則。

## 從另一台電腦使用

```bash
ssh -L 8099:127.0.0.1:8099 -L 8123:127.0.0.1:8123 your-user@castle-agentic-home.local
sudo agentic-home access-key
```

瀏覽器開 `http://localhost:8099`；家庭是 `http://localhost:8123`。這兩個主機入口只聽本機，也可以在 **設定 → 主機維護** 登入 Tailscale 後使用相同 SSH tunnel。若已啟用有線家庭接入且讀回 HAOS 的 LAN 位址，同網段手機可另外開啟 HA 原生 LAN 網址並登入；外層管理中心不會因此公開到 LAN。接法與回復步驟見 [家庭區網接入](docs/HA_LAN.md)。

新安裝的區網名稱是 `castle-agentic-home.local`（mDNS）；既有主機保留原名稱，請看主機維護的「區網名稱」。目前位址、位址變更與合蓋運作檢查也會顯示在主機維護。要固定目前位址，在路由器的「DHCP 位址保留」中選這台主機並保留即可。網路、合蓋設定與既有安裝的正式更新限制見 [主機運作與網路](docs/APPLIANCE_POWER_NETWORK.md)。

## 已有 Ubuntu 24.04

在**要安裝的那台電腦**執行，不要在開發機執行：

```bash
tar -xf agentic-home-0.11.1-linux-amd64.tar
cd agentic-home
python3 host/install.py --profile laptop-amd64
sudo python3 host/install.py --profile laptop-amd64 --apply
```

不加 `--apply` 只列出會做的事。安裝會檢查版本、架構、KVM、記憶體、空間和檔案 hash，不會覆蓋已存在的 `/opt/agentic-home`。沒有桌面的主機用 `--profile server-amd64`。

## 救援

Web 或 AI 不能用時，在主機的本機終端：

```bash
sudo agentic-home diagnostics
sudo agentic-home support-bundle --output ./agentic-support.json
```

support bundle 只含固定、去密的狀態，不含記錄原文、對話或憑證，也不會自動上傳。

Home Assistant 的固定操作：

```bash
agentic-home status
agentic-home start
agentic-home shutdown
agentic-home snapshot
agentic-home restore --snapshot <完整快照ID>
```

冷快照會先正常關閉家庭系統，期間家庭自動化暫停。快照含家庭資料，離機保存請加密。更多情況見 [救援](docs/OPERATIONS.md)。

## 範圍

- 外層主機可在選定的實體 IPv4 網路收集鄰居表、mDNS／SSDP。HAOS 預設基本 NAT，0.10.0 另提供明確選擇的有線接入；保留原管理網卡，不改主機 IP。保存接線、guest 位址、HA adapter 和實體設備動作分別讀回／驗證。
- 精確媒體與合成區網上的巢狀 HAOS 結果見 [交付摘要](evidence/laptop-d0089a8/summary.json)。Matter／Thread、Cast 播放和實體手機入口仍需各自驗收，不能僅憑配置保存或探索成功判定可用。
- USB dongle、Wi-Fi 驅動及實體設備的每項功能仍需現場確認；[設備探索](docs/DEVICE_DISCOVERY.md) 說明來源與接入範圍。
- 實體筆電的散熱、網卡和 UEFI 需要現場驗證。
- VM 快照、Home Assistant 備份、主機 Controller、智能管家和各 AI 登入是不同的備份層。

架構與驗證：[Linux 架構](docs/LINUX_PRODUCT.md) · [通訊](docs/GATEWAY.md) · [驗證紀錄](docs/VERIFICATION.md)。
