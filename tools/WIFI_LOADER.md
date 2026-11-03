# Wi-Fi加载器范围

load_tplink_wifi.py必须与其SHA256.json、modules/和固件在同一发布目录使用，不是单独拷贝即可加载的通用驱动。
比赛包optional_wifi附原v2发布ZIP；公开版只保留自研校验/加载逻辑。该脚本默认只读核对，只有用户显式--load才操作网卡。
