# 操作手册(图文版 · 给不写代码的使用者)

电镜采集 + 实时 QC 软件的日常操作。**不需要懂代码**,照着图上的红圈数字点就行。

> 截图是模拟模式下渲染的,界面布局和真机**完全一样**(真机会有实时数据)。

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

提示符前出现 `(venv)` = 环境对了。窗口弹出后是这样(下图是 HI-MC 页):

![总览](docs/operator/01_overview.png)

整体布局:**左**=系统状态/连接,**顶**=四个标签页,**中**=当前页,**右**=参数面板(Load/Save JSON)。
图上红圈对应下面的步骤:**①** 切到 HI-MC 页 · **②** 连机 · **③** 导入 JSON · **④** 开始采集。

---

## 2. 连接电镜 (图中 ②)

左边 **System Status** 面板:
1. 把 **Simulation Mode** 的勾 **去掉**(去掉=连真机;勾着=只模拟不动硬件)。
2. 点 **Connect**。
3. 上方状态变绿、真空/压力等数值开始刷新 = 连上了。

---

## 3. 导入实验 JSON (图中 ③)

右边 **Parameters** 面板 → 点 **📂 Load JSON** → 选这次的 imaging JSON(Zeiss 实验文件,含 regions + support points)。
导入后,路径会**自动填进** HI-MC 和 SFN 页,不用再选第二遍。

---

## 4.(换 wafer / 重新装片后)先做 SFN 重对位

> 只有**重新装载样品**、坐标系变了时才需要。同一片继续采 → 跳过这步。

点顶部 **SFN** 标签:

![SFN 页](docs/operator/03_sfn.png)

**Stage A — 跑校准(会动镜子)**
- **① Markers JSON**:选上一轮的 `markers_prev.json`(里面是 3 个 L-marker 的坐标)。**没有它跑不出有意义的结果。**
- 下面 Output dir / 扫描参数一般用默认。
- 点 **② ▶ Run calibration**。软件自动去 3 个 marker、拍图、找到它们、算出变换矩阵 `affine.json`;日志出现 `Affine written to: ...` = 成功(路径自动填到下面)。
- 要中途停:点 **■ Stop**。

**Stage B — 把矩阵套到旧 JSON(不动镜子)**
- Affine JSON / Old imaging JSON 已自动填好。
- 点 **③ Apply affine → all regions**(一次性把所有 region 坐标对正)。
- 成功后点 **④ Send new JSON → HI-MC**,对位好的新 JSON 就送进 HI-MC 页了。

---

## 5. HI-MC — 采集 + 实时 QC(主流程)

点顶部 **HI-MC** 标签,从上往下填:

![HI-MC 页](docs/operator/02_himc.png)

- **① Imaging JSON**:已自动填(或自己 Browse)。
- **② Raw focus-map dir**:指向**原始数据文件夹**(里面有 `slice_<N>_FocusMap.txt`,每段一个)。软件按 section 号自动匹配,**不用手动改名**。
- **③ Output root**:采集结果存哪。
- **④ Region selection**(二选一):
  - 采**整片**:勾 **Process ALL regions in the JSON**。开始前软件会**先检查每段的 focus-map 都在**;缺了默认**直接报错不开采**。想"缺的就跳过、继续采其它"→ 勾下面那行 **On missing focus-map: skip that region**。
  - 只采**一个 region**:**Region name** 填如 `S25R1`(此时别勾 Process ALL)。
- 其它参数(Scan / Route / Live QC / Stage / Autofocus / Autostig…)默认即可,要调直接改;最底下 **Advanced overrides** 可 `键 = 值` 设任意高级参数。
- **⑤ ▶ Start** 开始。下面 **System Logs** 实时滚动,进度条显示 `region x/总数 — tile a/b`。要停点 **■ Stop**(别直接关窗口)。
- **⑥ Open QC output…**:一段采完后,一键打开该段的 QC 文件夹。

---

## 6. 结果在哪

```
<Output root>\
  w13_h25_Slice1_8NM_20260629_143012\     ← 一个 block(名 = JSON名 + 像素 + 时间)
    S25R1\                                 ← region
      mfov42\                              ← 每个 MFOV 一个文件夹
        mfov42_<时间>__001.bmp             ← beam 图(__后三位 = beam 号)
        ...
        overview_mfov.png                  ← 该 MFOV 的降采样小图
      LiveQC\
        live_qc_dashboard.png / .html      ← 5-panel 总览 + 可点的 retake 页
        live_qc_results.csv                ← 每个 MFOV 一行
        live_qc_region_overview.png        ← 整段降采样总览
```

---

## 7. 常见情况

| 现象 | 怎么办 |
|---|---|
| 没看到 HI-MC / SFN 标签 | 没同步到最新。回第 0 步 `git pull`;确认版本不是旧的。 |
| 点 Start 提示没有 imaging JSON | 先做第 3 步 Load JSON。 |
| Process ALL 报 `[Preflight] FAILED` | 有段的 `slice_<N>_FocusMap.txt` 不在 Raw focus-map dir 里;补文件,或勾 "skip that region"。 |
| 采集中途要停 | 点 **■ Stop**(别直接关窗口)。 |
| 软件卡 / 报错 | 把 **System Logs** 里的红字截图发给开发。 |

---

## 8. 一句话速记

**同步 → 启动 → Connect(去掉 Simulation)→ Load JSON →(换片才做 SFN)→ HI-MC 填三个路径 → Start。**
