#!/bin/sh
# 在 Asuswrt-Merlin 上讓 CI 專用的 SSH 公鑰撐過重開機。
#
# 問題：Merlin 的 / 是每次開機從韌體重建的 ramdisk，而 root 的家目錄
#       （/root -> /tmp/home/root）在 tmpfs 上。放進 ~/.ssh/authorized_keys
#       的公鑰一重開機就消失，GitHub Actions 的 SOCKS5 通道隔天就連不上。
#
# 解法：公鑰存在持久分割區 /jffs，開機後由 /jffs/scripts/services-start
#       再把它合併回 root 的 authorized_keys。
#
# 用法（**在路由器上**執行）：
#   sh merlin-authorized-keys.sh install "ssh-ed25519 AAAA... cruise-deals-ci"
#   sh merlin-authorized-keys.sh apply     # 手動重跑合併（開機時會自動跑）
#   sh merlin-authorized-keys.sh status    # 檢查目前狀態（重開機後用這個驗收）
#
# ⚠️ 這支只處理**公鑰**（.pub）。私鑰永遠不該進到路由器——
#    它只該待在你自己的電腦，以及 GitHub 的 ROUTER_SSH_KEY secret 裡。
#
# 相容性：POSIX sh / busybox ash，不用 bash 語法。

set -e

# 這兩個可以用環境變數覆寫，純粹是為了能在開發機上把整套流程跑過一遍；
# router 上直接執行時用預設值就對了。
JFFS_DIR=${CRUISE_JFFS_DIR:-/jffs}
KEY_STORE="$JFFS_DIR/.ssh/authorized_keys"
HOOK_DIR="$JFFS_DIR/scripts"
HOOK="$HOOK_DIR/cruise-authorized-keys.sh"
SERVICES_START="$HOOK_DIR/services-start"
HOOK_LINE="[ -x $HOOK ] && $HOOK apply   # cruise-deals CI 通道公鑰"

# dropbear 不認得 OpenSSH 的 restrict 關鍵字，這組是它認得的等效寫法：
# 禁止配置終端機、禁止 agent 與 X11 轉發，只留下建立通道所需的埠轉發能力。
# 這把金鑰即使外洩，也開不了互動 shell。
RESTRICTIONS='no-pty,no-agent-forwarding,no-X11-forwarding'

die() { echo "錯誤：$*" >&2; exit 1; }
note() { echo "==> $*"; }
warn() { echo "⚠ $*" >&2; }

# root 的家目錄從 /etc/passwd 讀，不寫死 /root——不同韌體版本擺的位置不一樣。
# CRUISE_ROOT_HOME 一樣只是為了開發機上的測試。
root_home() {
  if [ -n "${CRUISE_ROOT_HOME:-}" ]; then
    echo "$CRUISE_ROOT_HOME"
    return 0
  fi
  home=$(awk -F: '$1 == "root" { print $6; exit }' /etc/passwd 2>/dev/null) || home=''
  [ -n "$home" ] || home=/root
  echo "$home"
}

# ---------------------------------------------------------------- apply

# 把 /jffs 上的公鑰合併回 root 的 authorized_keys。
# 刻意用「逐行比對後補上」而不是整份覆蓋，才不會洗掉從 WebUI／NVRAM
# 灌進來的其他金鑰（例如你平常登入用的那把）。
apply() {
  if [ ! -s "$KEY_STORE" ]; then
    note "$KEY_STORE 沒有內容，沒有東西要還原"
    return 0
  fi

  home=$(root_home)
  auth="$home/.ssh/authorized_keys"

  mkdir -p "$home/.ssh"
  chmod 700 "$home/.ssh"
  [ -f "$auth" ] || : > "$auth"
  chmod 600 "$auth"

  added=0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in '' | \#*) continue ;; esac
    if ! grep -qxF "$line" "$auth" 2>/dev/null; then
      printf '%s\n' "$line" >> "$auth"
      added=$((added + 1))
    fi
  done < "$KEY_STORE"

  logger -t cruise-authorized-keys "從 $KEY_STORE 補上 $added 把公鑰到 $auth" 2>/dev/null || true
  note "已合併到 $auth（本次新增 $added 行）"
}

# -------------------------------------------------------------- install

check_jffs() {
  [ -d "$JFFS_DIR" ] || die "找不到 $JFFS_DIR，這台機器可能沒有啟用 JFFS 分割區"
  if ! touch "$JFFS_DIR/.cruise-write-test" 2>/dev/null; then
    die "$JFFS_DIR 不可寫入。請到 WebUI 的 Administration → System 啟用 JFFS 分割區"
  fi
  rm -f "$JFFS_DIR/.cruise-write-test"

  # 沒開這個開關的話 /jffs/scripts/* 開機不會被執行，等於白裝
  enabled=$(nvram get jffs2_scripts 2>/dev/null) || enabled=''
  if [ "$enabled" != "1" ]; then
    warn "nvram jffs2_scripts 不是 1，開機鉤子不會被執行。"
    warn "請到 WebUI：Administration → System → Enable JFFS custom scripts and configs → Yes，"
    warn "或執行：nvram set jffs2_scripts=1 && nvram commit（之後要重開機）"
  fi
}

validate_key() {
  key=$1
  case "$key" in
    *"PRIVATE KEY"*)
      die "這看起來是**私鑰**。私鑰絕對不可以放上路由器——請改貼 .pub 檔的內容。"
      ;;
  esac
  echo "$key" | grep -qE '(^| )(ssh-ed25519|ssh-rsa|ecdsa-sha2-[a-z0-9]+|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-[a-z0-9]+@openssh\.com) [A-Za-z0-9+/]' \
    || die "看起來不像 SSH 公鑰（應含 ssh-ed25519／ssh-rsa／ecdsa-sha2-… 欄位）"
}

# 沒有帶限制選項時自動補上，別讓這把金鑰能開互動 shell
with_restrictions() {
  case "$1" in
    ssh-* | ecdsa-* | sk-*) printf '%s %s\n' "$RESTRICTIONS" "$1" ;;
    *) printf '%s\n' "$1" ;;
  esac
}

store_key() {
  entry=$1
  mkdir -p "$JFFS_DIR/.ssh"
  chmod 700 "$JFFS_DIR/.ssh"
  [ -f "$KEY_STORE" ] || : > "$KEY_STORE"
  chmod 600 "$KEY_STORE"

  if grep -qxF "$entry" "$KEY_STORE" 2>/dev/null; then
    note "$KEY_STORE 裡已經有這把金鑰，不重複加入"
  else
    printf '%s\n' "$entry" >> "$KEY_STORE"
    note "公鑰已存進 $KEY_STORE"
  fi
}

install_hook() {
  mkdir -p "$HOOK_DIR"

  # 把自己複製過去當開機鉤子。從安裝好的位置重跑時就不用複製了。
  # 順手去掉行尾的 CR——這支腳本是在 Windows 上編輯的，沾到 CRLF 的話
  # busybox 會在每一行都報 command not found，而且錯誤訊息看不出原因。
  self=$0
  if [ "$self" != "$HOOK" ]; then
    sed 's/\r$//' "$self" > "$HOOK"
    note "鉤子已安裝到 $HOOK"
  fi
  chmod 755 "$HOOK"

  # services-start 可能早就有別的內容，只在缺這一行時「附加」，絕不覆蓋
  if [ ! -f "$SERVICES_START" ]; then
    printf '#!/bin/sh\n' > "$SERVICES_START"
    note "建立了 $SERVICES_START"
  elif [ ! -s "$SERVICES_START" ]; then
    printf '#!/bin/sh\n' > "$SERVICES_START"
  fi

  if grep -qF "$HOOK apply" "$SERVICES_START" 2>/dev/null; then
    note "$SERVICES_START 已經掛好了"
  else
    printf '%s\n' "$HOOK_LINE" >> "$SERVICES_START"
    note "已把還原指令附加到 $SERVICES_START"
  fi
  chmod 755 "$SERVICES_START"

  head -n 1 "$SERVICES_START" | grep -q '^#!' \
    || warn "$SERVICES_START 第一行不是 shebang，Merlin 可能不會執行它，請自行檢查"
}

install_key() {
  key=$1
  [ -n "$key" ] || die "請把公鑰整行當參數傳進來（用單引號或雙引號包住）"

  validate_key "$key"
  check_jffs
  store_key "$(with_restrictions "$key")"
  install_hook
  apply

  echo
  note "完成。重開機後用下面這行驗收："
  echo "    sh $HOOK status"
}

# --------------------------------------------------------------- status

status() {
  home=$(root_home)
  auth="$home/.ssh/authorized_keys"

  echo "root 家目錄          : $home"
  echo "持久公鑰存放         : $KEY_STORE $([ -s "$KEY_STORE" ] && echo "（$(grep -cve '^\s*$' "$KEY_STORE" 2>/dev/null || echo 0) 行）" || echo '（不存在或空的）')"
  echo "開機鉤子             : $HOOK $([ -x "$HOOK" ] && echo '（可執行）' || echo '（缺少或不可執行）')"
  echo "services-start 掛載  : $(grep -qF "$HOOK apply" "$SERVICES_START" 2>/dev/null && echo '已掛上' || echo '**沒掛上**')"
  echo "nvram jffs2_scripts  : $(nvram get jffs2_scripts 2>/dev/null || echo '讀不到')"
  echo "目前生效的 authorized_keys : $auth"

  if [ ! -s "$KEY_STORE" ]; then
    echo
    warn "還沒有存任何公鑰，先跑 install"
    return 0
  fi

  missing=0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in '' | \#*) continue ;; esac
    grep -qxF "$line" "$auth" 2>/dev/null || missing=$((missing + 1))
  done < "$KEY_STORE"

  echo
  if [ "$missing" -eq 0 ]; then
    echo "✓ /jffs 上的公鑰目前都已生效"
  else
    warn "有 $missing 行公鑰還沒生效，執行 $HOOK apply 補上"
  fi
}

# ----------------------------------------------------------------- main

case "${1:-}" in
  install) shift; install_key "${1:-}" ;;
  apply) apply ;;
  status) status ;;
  *)
    cat >&2 <<'USAGE'
用法：
  sh merlin-authorized-keys.sh install "ssh-ed25519 AAAA... cruise-deals-ci"
      把公鑰存進 /jffs、安裝開機鉤子，並立刻生效

  sh merlin-authorized-keys.sh apply
      手動把 /jffs 上的公鑰合併回 root 的 authorized_keys（開機時自動執行）

  sh merlin-authorized-keys.sh status
      檢查目前狀態，重開機後用這個驗收

只接受公鑰。私鑰請留在自己的電腦與 GitHub secret 裡。
USAGE
    exit 2
    ;;
esac
