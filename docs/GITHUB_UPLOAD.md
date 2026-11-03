# 上传GitHub

上传目录是01_GitHub源码/pcb-vision，勿上传整个桌面交付文件夹或比赛提交包。

```sh
git init
git add .
git commit -m "Initial P9 source release"
git branch -M main
git remote add origin https://github.com/YOUR_ACCOUNT/pcb-vision.git
git push -u origin main
```

先在自己的GitHub账号创建空仓库，用自己的凭据登录。上述操作由用户执行；本次整理未创建仓库、未提交或推送。LICENSE采用用户选择的MIT，第三方例外见LICENSE_SCOPE.md。发布位流、模型或完整厂商工程前，分别处理其许可，不要只靠根目录MIT。
MANIFEST.json是整理时的文件哈希快照。后续改文件后应重新生成，不能用旧哈希验证新版本。
