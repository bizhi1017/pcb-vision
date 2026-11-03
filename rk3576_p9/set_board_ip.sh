#!/bin/bash
# ============================================================================
# set_board_ip.sh —— 给板卡有线网口设置**持久化**静态 IP（重启后仍有效）
#
# 为什么需要它：
#   `ip addr add ...` 只是临时设置，板卡一重启（或 NetworkManager 重新应用配置）
#   地址就没了 —— 表现为"以太网又连不上板卡"。
#
# 自动适配三种网络管理器：NetworkManager / systemd-networkd / ifupdown
#
# 用法（板卡上）：
#   sudo bash set_board_ip.sh                  # 默认 end0 + 192.168.10.50/24
#   sudo bash set_board_ip.sh end0 192.168.10.50/24
#   sudo bash set_board_ip.sh --status         # 只看当前状态
#
# 注意：不要用 192.168.1.x —— 绝大多数家用 WiFi 路由器占这个网段，
#       PC 会把包从 WiFi 发出去，导致永远 ping 不通板卡。
# ============================================================================
set -u

IF="${1:-end0}"
ADDR="${2:-192.168.10.50/24}"

if [ "${IF}" = "--status" ]; then
    echo "== 网口状态 =="
    ip -br link
    echo
    ip -br addr
    echo
    echo "== 网络管理器 =="
    for s in NetworkManager systemd-networkd networking; do
        printf '  %-18s %s\n' "$s" "$(systemctl is-active $s 2>/dev/null || echo unknown)"
    done
    exit 0
fi

IP="${ADDR%/*}"
PREFIX="${ADDR#*/}"

echo "[1/3] 目标: 网口 $IF, 地址 $ADDR (前缀 $PREFIX)"
if ! ip link show "$IF" >/dev/null 2>&1; then
    echo "[ERR] 找不到网口 $IF。可用网口:"
    ip -br link | awk '{print "        " $1}' | grep -v '^        lo$'
    exit 1
fi

MANAGER=""
for s in NetworkManager systemd-networkd networking; do
    if systemctl is-active --quiet "$s" 2>/dev/null; then
        MANAGER="$s"
        break
    fi
done

case "$MANAGER" in
# ---------------------------------------------------------------- NetworkManager
NetworkManager)
    echo "[2/3] 网络管理器: NetworkManager"
    CON="$(nmcli -t -f NAME,DEVICE con show 2>/dev/null | awk -F: -v d="$IF" '$2==d{print $1; exit}')"
    if [ -z "$CON" ]; then
        CON="static-$IF"
        nmcli con add type ethernet ifname "$IF" con-name "$CON" \
              ipv4.method manual ipv4.addresses "$ADDR" \
              ipv4.gateway "" ipv4.dns "" >/dev/null
    else
        nmcli con mod "$CON" ipv4.method manual ipv4.addresses "$ADDR" \
              ipv4.gateway "" ipv4.dns "" >/dev/null
    fi
    nmcli con up "$CON" >/dev/null
    echo "        连接名: $CON （已设为开机自动应用）"
    ;;
# ---------------------------------------------------------------- systemd-networkd
systemd-networkd)
    echo "[2/3] 网络管理器: systemd-networkd"
    F="/etc/systemd/network/10-${IF}-static.network"
    cat > "$F" <<EON
[Match]
Name=$IF

[Network]
Address=$ADDR
DHCP=no
EON
    chmod 644 "$F"
    systemctl restart systemd-networkd
    echo "        配置: $F"
    ;;
# ---------------------------------------------------------------- ifupdown
networking)
    echo "[2/3] 网络管理器: ifupdown (/etc/network/interfaces)"
    cp /etc/network/interfaces "/etc/network/interfaces.bak.$(date +%s)" 2>/dev/null || true
    if grep -q "iface $IF inet static" /etc/network/interfaces 2>/dev/null; then
        sed -i "/iface $IF inet static/,/^$/s|address .*|address $IP|" /etc/network/interfaces
    else
        cat >> /etc/network/interfaces <<EOI

auto $IF
iface $IF inet static
    address $IP
    netmask 255.255.255.0
EOI
    fi
    ifdown "$IF" 2>/dev/null || true
    ifup "$IF" 2>/dev/null || true
    echo "        已写入 /etc/network/interfaces（原文件已备份）"
    ;;
# ---------------------------------------------------------------- 兜底
*)
    echo "[2/3] 没识别到网络管理器，只做临时设置（重启会失效！）"
    ip addr flush dev "$IF" 2>/dev/null || true
    ip addr add "$ADDR" dev "$IF"
    ip link set "$IF" up
    echo "        如果重启后仍需要，请手动把它写进启动脚本（rc.local / systemd unit）"
    ;;
esac

echo "[3/3] 当前状态:"
ip -br addr show "$IF"
echo
echo "自检（本机访问自己，需先启动 --http 服务）:"
echo "    curl -s http://127.0.0.1:8000/api/status ; echo"
echo "对端（PC 设 192.168.10.100/24 后）:"
echo "    ping -c 3 192.168.10.100"
