module tailit/app

go 1.22

// Minimal — stdlib only for now. Add sing-box/xray when you need proxy checks:
// require (
//   github.com/sagernet/sing-box v1.11.3
//   github.com/xtls/xray-core v1.8.11
// )
// sing-box covers most protocols (vmess/vless/trojan/shadowsocks/hysteria/wireguard);
// keep xray only for reality/xtls-rprx-vision if needed.
