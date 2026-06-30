# 操作手册(给不写代码的使用者)

电镜采集 + 实时 QC 软件的日常操作。**不需要懂代码**,照着点就行。命令都是复制粘贴。

---

## 0. 每次开始:同步最新版(sync)

打开 **PowerShell**,粘贴(整段):

```powershell
cd D:\mSEMAPI\himc_app
git pull origin gui-integration
```

> 第一次用、还没有 `himc_app` 文件夹?先建一次(只做一次):
> ```powershell
> cd D:\mSEMAPI
> git clone -b gui-integration https://github.com/FumingYang-Felix/Auto-multisem-control-and-QC.git himc_app
> ```

看到 `Already up to date.` 或 `Updating ...` 都正常。

---

## 1. 启动软件

接着在同一个 PowerShell 里粘贴(整段):

```powershell
D:\mSEMAPI\mSEMAPI\venv\Scripts\Activate.ps1
cd D:\mSEMAPI\himc_app
python -m msem_control.main
```

提示符前面出现 `(venv)` = 环境对了。窗口会弹出来,顶部有四个标签:
**Control & Map** · **Acquisition** · **HI-MC** · **SFN**

> 那条 `SetProcessDpiAwarenessContext ... 0x57` 是无害提示,忽略。

---

## 2. 连接电镜

左边 **System Status** 面板:
1. 把 **Simulation Mode** 的勾 **去掉**(去掉=连真机;勾着=只是模拟,不动硬件)。
2. 点 **Connect**。
3. 上面状态变绿、压力/真空等数值开始刷新 = 连上了。

---

## 3. 导入实验 JSON

右边 **Parameters** 面板 → 点 **Load JSON** → 选这次的 imaging JSON(就是 Zeiss 的实验文件,含 regions + support points)。
导入后,JSON 路径会**自动填进** HI-MC 和 SFN 页对应的框,不用再选第二遍。

---

## 4.(换 wafer / 重新装片后)先做 SFN 重对位

> 只有**重新装载样品**、坐标系变了的时候才需要。同一片继续采,跳过这步。

点顶部 **SFN** 标签。分两步:

### Stage A — 跑校准(动镜子)
1. **Markers JSON**:选上一轮的 `markers_prev.json`(里面是 3 个 L-marker 的坐标)。**没有它跑不出有意义的结果。**
2. **Output dir**:校准结果(affine.json)存哪。
3. 扫描参数(Scan rate / Pixel size / Tile width/height)一般用默认。
4. 点 **▶ Run calibration**。软件会自动去 3 个 L-marker、拍图、找到它们,算出一个变换矩阵 `affine.json`。
5. 下面日志跑完、出现 `Affine written to: ...` = 成功。这个路径会**自动填到 Stage B**。
   - 中途要停:点 **■ Stop**。

### Stage B — 把矩阵套到旧 JSON(不动镜子)
1. **Affine JSON**:Stage A 跑完会自动填好。
2. **Old imaging JSON**:第 3 步 Load 的那个,会自动填好。
3. 点 **Apply affine → all regions**(一次性把所有 region 的坐标都对正)。
4. 成功后点 **Send new JSON → HI-MC**,新的(已对位的)JSON 就送进 HI-MC 页了。

---

## 5. HI-MC — 采集 + 实时 QC(主流程)

点顶部 **HI-MC** 标签,从上往下填:

**Input / Output**
- **Imaging JSON**:已自动填(或自己 Browse)。
- **Raw focus-map dir**:指向**原始数据文件夹**(里面有 `slice_<N>_FocusMap.txt`,每个 section 一个)。软件按 section 号自动匹配,**不用手动改名**。
- **Output root**:采集结果存哪。

**Region selection**(二选一)
- 采**整片**:勾 **Process ALL regions in the JSON**。
  - 勾上后软件会在开始前**先检查每个 section 的 focus-map 都在**;缺了默认**直接报错不开始**(省得跑一半才发现)。想"缺的就跳过、继续采其它的",勾上 **On missing focus-map: skip that region**。
- 只采**一个 region**:**Region name** 填如 `S25R1`(此时不要勾 Process ALL)。

**其它参数**(Scan / Route / Live QC / Stage / Autofocus / Autostig…)
一般用默认即可;要调就直接在对应框里改。最底下的 **Advanced overrides** 框可以 `键 = 值` 一行一个,设任何没列出来的高级参数。

**开始**
- 点 **▶ Start**。下面 **System Logs** 会实时滚动,进度条显示 `region x/总数 — tile a/b`。
- 要停:点 **■ Stop**(优雅停止,会把当前这张图收尾、QC 收好再退)。
- 一段采完后,点 **Open QC output…** 直接打开该段的 QC 输出文件夹。

---

## 6. 结果在哪

```
<Output root>\
  w13_h25_Slice1_8NM_20260629_143012\     ← 一个 block(名字 = JSON名 + 像素 + 时间)
    S25R1\                                 ← region
      mfov42\                              ← 每个 MFOV 一个文件夹
        mfov42_<时间>__001.bmp             ← beam 图(__后面三位 = beam 号)
        ...
      LiveQC\                              ← 该段的实时 QC
        live_qc_dashboard.png / .html      ← 5 panel 总览 + 可点的 retake 页
        live_qc_results.csv                ← 每个 MFOV 一行
        live_qc_region_overview.png        ← 整段降采样总览
      mfov42\overview_mfov.png             ← 单个 MFOV 的降采样小图
```

---

## 7. 常见情况

| 现象 | 怎么办 |
|---|---|
| 没看到 HI-MC / SFN 标签 | 没同步到最新。回到第 0 步 `git pull`,确认 `git rev-parse --short HEAD` 不是旧版本。 |
| 点 Start 提示没有 imaging JSON | 先做第 3 步 Load JSON。 |
| Process ALL 报 `[Preflight] FAILED` | 有 section 的 `slice_<N>_FocusMap.txt` 不在 Raw focus-map dir 里;补上文件,或勾 "skip that region"。 |
| 采集中途要停 | 点 **■ Stop**(别直接关窗口)。 |
| 软件卡 / 报错 | 把 **System Logs** 里的红字截图发给开发。 |

---

## 8. 一句话速记

**同步 → 启动 → Connect(去掉 Simulation)→ Load JSON →(换片才做 SFN)→ HI-MC 填三个路径 → Start。**
