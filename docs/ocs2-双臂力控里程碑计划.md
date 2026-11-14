# BR-R73R 双臂 OCS2 力控：里程碑计划

- 对象：中科新松 BR-R73R 双七轴裸臂（无 DucoCore 控制柜），关节为 EtherCAT 从站，末端为因时 RH56F1 灵巧手。URDF `art7by_arm`，可控关节 `left_joint1`–`7`、`right_joint1`–`7`，共 14 个。
- 已有基础：双轮 VMC、LQR、PID、减速电机三环 PI、MoveIt2 笛卡尔规划。
- 目标：自建 EtherCAT 主站，在双臂上加入 OCS2 实时 MPC，并把关节扭矩传感器反馈接入控制回路。

## 1. 硬件边界与方案形态

没有控制柜，`servoj`、`fc_*`、`get_tcp_force` 这类 DucoCore SDK 接口都不存在。我们自己是 EtherCAT 主站，关节从站对外开放的过程数据决定了方案形态。

### 1.1 关节从站 `SERVO_ECAT_DR3`（ESI `SERVO_ECAT_DR3_0.1.8.xml` + 《DR 系列机器人伺服软件接口说明书》V05，2026-04）

- Vendor `0x8CF`（SIASUN），Product `0x39252`，Rev 8，Device type `0x00020192`（CiA402）。
- CoE：SDO Info、Segmented SDO、Complete Access；FoE（更新固件必需）。支持 DC 同步（`AssignActivate 0x300`），PDO 周期 1 / 2 / 4 ms。
- PDO 映射 `Fixed`，输出 42 字节、输入 40 字节。ESI 字典里 `0x1600/0x1A00` 的默认子项是旧的，以 `RxPdo/TxPdo` 元素和接口说明书为准。
- 运行模式（`0x6060` / `0x6061`）：8 CSP、9 CSV、10 CST（电机侧）、**−11 CSJT 关节扭矩闭环**（带扭矩传感器版本支持）；1、4 预留。
- 固件版本经 SDO `0x2035`（软件）、`0x2036`（参数）读取；扭矩环 PDO 自接口说明书 V03（2025-11）起存在，错误码表对应固件 3.1.x 以上。

主站 → 关节（RxPDO `0x1600`）：

| 对象 | 名称 | 单位 / 含义 |
| --- | --- | --- |
| `0x6040` / `0x6060` | Control word / Modes of operation | 标准 CiA402 |
| `0x607A` | Target position | cnt，低速侧一圈 2¹⁹，仅 CSP |
| `0x60FF` | Target velocity | cnt/s，高速端，仅 CSV |
| `0x6071` | Target torque | 电机额定扭矩千分比，高速端，仅 CST |
| `0x700A` | TargetJointTorque | 低速侧关节扭矩，N·m×1000，仅 CSJT(−11) |
| `0x3B6D` | TorqueSensorOffset | 扭矩传感器零点，N·m×1000；启用 −11 前在近零负载姿态写入当时的 `0x3B69` 读数 |
| `0x60B2` | TorqueOffset | 电流前馈，单位同 `0x6071`，仅在非电流环、非扭矩环模式（CSP/CSV）生效 |
| `0x2201` | JointInertia | 折算到低速侧的负载惯量，kg·m²×1000 |
| `0x2202` | GravityOffset | 重力矩 + 科氏力矩，N·m×10000 |
| `0x7007` | TorqueBand | 扭矩传感器限幅，千分比，高 16 位上限、低 16 位下限；0 时按 tImax 饱和 |
| `0x7008` | CurrentBand | 电机电流限幅，同格式；0 时按 cImax 饱和 |
| `0x60FE` | Digital outputs | 实际为 tOb_Bandwidth 指定值（Hz），不是抱闸位 |
| `0x20A4` | — | 字节对齐，无功能 |

关节 → 主站（TxPDO `0x1A00`）：

| 对象 | 名称 | 单位 / 含义 |
| --- | --- | --- |
| `0x6064` | Actual position | 高速端编码器 cnt，低速侧一圈 0–2¹⁹ |
| `0x20A0` | Load-side encoder position | 低速侧编码器，19 bit，cnt |
| `0x606C` / `0x6069` | 速度 | cnt/s，高速端 / 低速端 |
| `0x6077` | Torque actual | 电机扭矩千分比，高速端 |
| `0x6078` | Current actual | 电机电流千分比 |
| `0x3B69` | Torque sensor | 低速侧实际扭矩，mN·m |
| `0x60F4` | Following error | 低速端，cnt/s（文档原文） |
| `0x6041` / `0x6061` | 状态字 / 模式回显 | 标准 CiA402 |
| `0x700B` | Error code | 32 位，每位一类错误，位表见接口说明书第 8 章（图片） |
| `0x60FD` | Digital inputs | 高 8 位温度、低 8 位母线电压 |

SDO 参数（CSJT 用）：

| 对象 | 名称 | 单位 / 建议值 |
| --- | --- | --- |
| `0x7011` | TorqueLoopBW | 扭矩环带宽，Hz×100，≤3000 |
| `0x7012` | TorqueLoopDampRatio | 扭矩环阻尼比，×100，≤80；与 `0x7011` 必须同时写 |

接口说明书未写、仍需向厂家确认的项：

- 抱闸由谁控制、何时释放/吸合；−11 模式下使能与下使能时的抱闸行为。文档里没有抱闸控制位。
- `0x2201` / `0x2202` 在哪个模式下生效，是前馈还是伺服内部补偿；不下发时默认值。
- `0x700B` 错误码位定义（文档中为图片）。
- 切换到 −11 的条件：允许的 402 状态、是否要求 `0x700A = 0`。
- 各关节额定/峰值扭矩、减速比、连杆质量质心。
- `0x7011` / `0x7012` 出厂默认值。
- 主站示例代码（TwinCAT / SOEM / IgH）。

### 1.2 灵巧手 `RH56F1`（ESI `EtherCAT_RH56F1_IO_V4.0.xml`）

- 因时 Inspire RH56，MDP profile 5001，六个指关节电机。
- 输出 `0x7000`：`ENABLE_SET`、`ANGLESET1–6`、`FORCESET1–6`、`SPEEDSET1–6`。
- 输入 `0x6000`：`POSACT`、`ANGLEACT`、`FORCEACT`、`CURACT`、`ERROR`、`STATUS`、`TEMP` 各 6 路。
- 触觉阵列 `0x6010/0x6020/0x6030` 三组共 364 个 INT16，对应 URDF 中 `*_force_sensor` 网格。
- 配置 `0x2000`：电流限制、默认速度/力、`FINGER_MODE1–6`、手势力校准；`0x8001 Work_Mode`、`0x8020–0x8022` 同步/异步模式与位置误差上限。
- 指力闭环在手内：设角度加力阈值，到力即停。主站只把 `FORCESET` 当抓握力上限用，不写手的力环。
- 输入 SM 默认 `0x100` 字节装不下三组触觉 PDO，`0x1C13` 分配需在主站里确认。

### 1.3 方案形态

- 底座：IgH EtherCAT Master + `ethercat_driver_ros2`。关节从站 PDO 固定且带厂商对象，用 `GenericEcSlave` 按上表写 YAML，不用 `EcCiA402Drive` 默认映射；手单独一个 `GenericEcSlave`。
- OCS2 做带关节限位、自碰撞、双臂避碰的实时 MPC，输出参考关节轨迹或末端位姿。
- 力控目标模式：**CSJT（−11）关节扭矩闭环**。理由：闭环在低速侧扭矩传感器上，减速器摩擦与减速比误差在环内；`0x700A` 与 `0x3B69` 同为 mN·m，无需换算；厂家提供扭矩环带宽/阻尼比接口，说明其自身拖动示教与力控走的就是这条路。CST(10) 闭的是电机电流，需要自行建减速器摩擦模型，不选。
- 控制结构（每关节 1 kHz）：\(\tau_{0x700A} = g(q) + \tau_{\text{阻抗}} + \tau_{\text{前馈}}\)。\(g(q)\) 由主站 Pinocchio 计算，第一版全部放入 `0x700A`，`0x2202` 保持 0；确认 `0x2202` 语义后再决定是否把重力项挪给伺服。外力估计 \(\tau_{ext} = 0x3B69 - g(q)\)。
- 过渡路线：CSP（`0x607A`）+ 导纳，`0x3B69 - g(q)` 做外力输入，主站积分出位置。用于在重力模型和安全层成熟前验证基础设施，之后作为 CSJT 不可用时的兜底。这一层直接用 [FZI cartesian_controllers](https://github.com/fzi-forschungszentrum-informatik/cartesian_controllers/tree/ros2)（ROS 2 分支）的 `cartesian_compliance_controller` / `cartesian_force_controller`，不自己写导纳。
- FZI 与 OCS2 的关系：FZI 是 1 kHz 笛卡尔伺服，每周期把位姿误差和力误差解成关节位置或速度，写 `0x607A`。它没有预测时域，不做自碰撞和双臂避碰。OCS2 是参考生成器，把末端目标、关节限位和碰撞约束解成参考位姿，交给 FZI 或 CSJT 阻抗去跟踪。两者叠用，不互相替换。FZI 不出力矩，驱动不了 `0x700A`，所以到不了 CSJT 目标方案。FZI 要的是 6 维力（`ft_sensor_ref_link`），本臂没有腕部六维力，需要把 \(\tau_{ext} = 0x3B69 - g(q)\) 经 \(J^{+T}\) 换成末端 wrench 再喂给它，重力模型仍然要做。
- 安全层全部自建：无碰撞检测、无安全控制器、无安全板接口（`0x20A4` 仅为对齐字节）。`0x7007/0x7008` 限幅带、`0x700B` 错误位、主站看门狗、软限位、速度限幅是全部保护手段。
- CSJT 下 `0x700A = 0` 即关节完全松弛，J2/J4 会在重力下坠落。首次切 −11 只能在 J1 或 J7 做，整臂切换前重力模型必须到位，且要先确认抱闸行为。

### 1.4 已在实机上确认的事实

- 主站为 IgH，两个 master，每链拓扑：`SERVO_ECAT_DR3` ×7 → `ECAT_EndIO`（末端 IO 板）→ `GENERIC_ACTUTOR`（RH56F1）。
- 前人程序以位置指令运行，全部从站 OP。
- SDO 读 `0x6502` 返回 `0x06020000`（对象不存在），与接口说明书一致（未开放该对象）。SDO 读 `0x6061` 返回 0，需用 `ethercat domains -v` 核对过程数据里的模式回显字节。
- 已取得《DR 系列机器人伺服软件接口说明书》V05，第 1.1 节的单位与模式表均来自该文档。

### 1.5 仍需向新松确认的清单

接口说明书已回答模式表、各力矩对象单位、扭矩传感器单位与零点流程、编码器分辨率、限幅带格式、SDO 版本对象。剩余问题：

1. 抱闸：由谁控制、使能/下使能时释放与吸合时序；−11 模式下使能、下使能、故障时的抱闸行为；J5–J7 无抱闸时掉电行为。
2. `0x2201` JointInertia、`0x2202` GravityOffset：在哪个模式下生效，是前馈项还是伺服内部补偿；不下发时默认值；更新周期要求。
3. `0x700B` 32 位错误码的位定义（文档第 8 章为图片）与故障复位流程。
4. 切换到 −11 的条件：允许在哪个 402 状态切换、是否要求 `0x700A = 0`、CSP↔CSJT 在线切换是否允许。
5. `0x7011` / `0x7012` 扭矩环带宽与阻尼比的出厂默认值。
6. `0x7007` / `0x7008` 超限后的从站动作（限幅还是报错停机）。
7. 总线丢帧或主站停止时的看门狗动作；从站内置的软限位、速度限幅、过温过流阈值能否修改。
8. 各关节额定扭矩、峰值扭矩、减速比、转子惯量；各连杆质量、质心、惯量张量或官方 URDF；关节摩擦参数。
9. `ECAT_EndIO` 的 ESI 与功能说明。
10. 推荐 DC 配置（周期、Sync0 偏移）；过程数据生效延迟。
11. BR 系列主站示例代码；DucoCore 对该臂做力控的参考参数；当前固件版本（`0x2035`）是否已含扭矩环，若无则固件更新渠道。

优先级：1、2、4 决定能否在整臂上启用 CSJT，先催这三条。

## 2. URDF 要点

- 腰、颈、腿、轮子、手指全部为 `fixed`，OCS2 以 `base_link` 为固定基座，只优化 14 个转动关节。
- 关节限位与样本一致：J1/J3/J5 ±170°，J2 ±100°，J4 −65°～+155°，J6 ±90°，J7 −75°～+90°。
- 力矩上限：J1/J2 100 N·m，J3/J4 50 N·m，J5–J7 20 N·m。
- J6→J7 沿轴向偏 75 mm，腕为异面十字，使用数值逆解。
- `left_joint7` 的 `rpy` 带约 0.5° 导出残差，保持原样，不要改成整齐的 π/2。
- 左右 J2 轴向相反，两臂不能共用同一套关节符号。
- 连杆惯量为 TGCAD 导出值，单臂合计约 12.6 kg，样本约 8.5 kg。走力矩路线前必须做动力学辨识。
- 碰撞网格为完整 STL，接 OCS2 自碰撞前先换成圆柱和盒子。
- `*_force_sensor` 网格对应 RH56F1 触觉阵列，数据从手的 TxPDO 读，不在 URDF 中。
- 末端帧：抓持用 `left_hand_tcp` / `right_hand_tcp`，对指用 `*_hand_pinch_tcp`，只控法兰用 `left_arm_link_7` / `right_arm_link_7`。

## 3. 参考项目

### EtherCAT 主站

- [IgH EtherCAT Master](https://gitlab.com/etherlab.org/ethercat)：Linux 内核态主站，`ethercat` 命令行用于扫描从站、读写 SDO、导出对象字典。
- [ICube-Robotics/ethercat_driver_ros2](https://github.com/ICube-Robotics/ethercat_driver_ros2)：ros2_control 硬件接口，`GenericEcSlave` 用 YAML 描述 PDO 通道到 state/command interface 的映射；`EcCiA402Drive` 管理 402 状态机，可参考其控制字/状态字处理。
- [SOEM](https://github.com/OpenEtherCATsociety/SOEM)：用户态主站，适合写最小验证程序确认模式值、单位和使能时序。

### OCS2 本体

- [leggedrobotics/ocs2](https://github.com/leggedrobotics/ocs2)，文档：[Getting Started](https://leggedrobotics.github.io/ocs2/getting-started.html)、[Optimal Control Modules](https://leggedrobotics.github.io/ocs2/optimal_control_modules.html)、[Robotic Examples](https://leggedrobotics.github.io/ocs2/robotic_examples.html)。重点读 `ocs2_mobile_manipulator` 的 `task.info`、`MobileManipulatorInterface`、`EndEffectorConstraint`、`JointVelocityLimits`、`SelfCollisionConstraint`、`MPC_MRT_Interface`。
- [legubiao/ocs2_ros2](https://github.com/legubiao/ocs2_ros2)：ROS 2 Humble/Jazzy 可编译，自带 dual-arm mobile manipulator 例子。

### OCS2 接 ros2_control 的模板

- [YangLuo-Bionics/arms_ros2_control](https://github.com/YangLuo-Bionics/arms_ros2_control)：`ocs2_arm_controller`（HOLD/HOME/OCS2 状态机、RViz 目标、位置输出）。
- [qiayuanl/legged_control](https://github.com/qiayuanl/legged_control)：OCS2 + ros_control，MPC 线程与实时线程分离，WBC 输出力矩，重点看 `LeggedController`。

### 力控层

- [ros2_controllers admittance_controller](https://github.com/ros-controls/ros2_controllers/tree/master/admittance_controller)：位置接口导纳，路线 1 的直接参考。
- [FZI cartesian_controllers](https://github.com/fzi-forschungszentrum-informatik/cartesian_controllers)：`cartesian_compliance_controller`、`cartesian_force_controller`，面向位置/速度接口。
- [matthias-mayr/Cartesian-Impedance-Controller](https://github.com/matthias-mayr/Cartesian-Impedance-Controller)：力矩接口笛卡尔阻抗，含零空间关节阻抗与末端期望力，路线 2 的直接参考。
- [yifan-hou/force_control](https://github.com/yifan-hou/force_control)：混合力/速度控制，理解选择矩阵与轴向切换。

### 力进 OCS2 优化问题

- [skywoodsz/qm_control](https://github.com/skywoodsz/qm_control) `feature-force` 分支、[danisotelo/qm_door](https://github.com/danisotelo/qm_door) `force-tracking` 分支：末端接触力写成 OCS2 的约束/代价，以及与 WBC 的任务分配。

### 动力学辨识

- [Pinocchio](https://github.com/stack-of-tasks/pinocchio)：正逆动力学、回归矩阵，重力项计算与参数辨识的基础。
- [FIGAROH](https://github.com/thanhndv212/figaroh) 或 [SymPyBotics](https://github.com/cdsousa/SymPyBotics)：激励轨迹设计与最小参数集辨识。

### 厂商侧

- 中科新松 [多可文档中心](https://docs.ducorobots.cn/zh/) 的接口/二次开发文档面向 DucoCore 六轴，对裸臂仅供理解其柜内力控做了什么（`get_ext_torque`、`fc_config` 参数定义）。
- 向厂家索取：`0x6060` 模式表、`0x3B69/0x700A/0x6071` 标定系数、编码器分辨率、抱闸与安全板位定义、使能时序、主站示例。
- 因时 RH56 用户手册：`FORCESET`、`ANGLESET` 单位与范围，触觉阵列排布。

## 4. 知识衔接

| 已有 | 对应到本项目 |
| --- | --- |
| LQR | OCS2 的 SLQ/iLQR 沿标称轨迹反复线性化后解时变 LQR，再滚动时域；Q、R 调参直觉用于 `task.info` |
| VMC | 路线 1 的导纳：力误差经虚拟质量、阻尼、刚度变成位移；路线 2 的阻抗：位姿误差经刚度、阻尼变成力矩 |
| 三环 PI | 电流、速度、位置环在关节从站内；CSP 下我们发位置目标，力矩模式下我们替代位置环直接发 `0x700A` |
| 减速电机驱动 | CiA402 状态机、控制字/状态字、模式切换与从站使能时序 |
| MoveIt2 | 离线轨迹，用于阶段 3 的自由空间验证；OCS2 每周期重解 |

## 5. 里程碑

按里程碑推进，每个阶段有可运行的结果后再进入下一阶段。

### 阶段 0：补理论

- 最优控制：离散 LQR → iLQR/DDP，时域、终端代价、软硬约束。
- 柔顺控制：阻抗 vs 导纳，力矩接口与位置接口下各自的稳定性条件。
- Pinocchio：`forwardKinematics`、`computeJointJacobians`、`getFrameJacobian`、`computeGeneralizedGravity`、`rnea`。
- EtherCAT/CiA402：DC 同步、PDO/SDO、402 状态机、CSP/CSV/CST。

产出：能说明 OCS2 的状态、输入、代价、约束在七轴臂上各是什么；能画出 402 状态机和本从站的 PDO 表。

### 阶段 1：跑 OCS2 例子

- 编译 `ocs2_ros2`，依次跑 double integrator、cartpole、mobile manipulator（Franka 七轴）、dual-arm。
- 改 `task.info`：Q/R、时域长度、`EndEffectorConstraint` 权重，观察肘部漂移与关节限位。
- 读 `MPC_MRT_Interface`，理解 MPC 线程与高频评估线程如何交换策略。

产出：用自写节点发末端目标，Franka 例子实时跟踪。

### 阶段 2：换成自己的双臂

- `art7by_arm` URDF 接入 dual-arm 例子，只保留 14 个转动关节，末端帧用 `left_hand_tcp` / `right_hand_tcp`。
- 碰撞网格换成圆柱和盒子，配置自碰撞对（两臂互相、臂与躯干）。
- 加关节姿态代价收肘，核对关节限位与实机一致。

产出：RViz 拖动两个目标，dummy 仿真中双臂跟踪且不互穿。

### 阶段 3：EtherCAT 主站与单关节

- 装 IgH 主站（PREEMPT_RT 内核）。读 SDO `0x2035/0x2036` 确认固件版本含扭矩环；`ethercat pdos`、`ethercat domains -v` 核对实际 PDO 顺序与模式回显字节。
- 向新松确认抱闸行为（1.5 节第 1 条）后再做单关节：402 使能、读 `0x6064/0x20A0/0x3B69`，按 2¹⁹ cnt/圈换算角度。
- CSP 模式下单关节小幅正弦跟踪，记录 `0x60F4` 与 `0x3B69` 零点漂移；设置 `0x7007/0x7008` 限幅带并触发一次，确认从站动作。
- 扭矩传感器零点：近零负载姿态读 `0x3B69`，写入 `0x3B6D`，确认读数归零。
- 单关节 CSJT 验证（选 J1 或 J7，重力矩近零）：`0x6060 = −11`、`0x700A = 0` 使能，关节应可手推；写 +500（0.5 N·m）确认 `0x20A0` 方向；主站 PD 外环做 5°→45° 五次多项式轨迹；力矩饱和限 10% 额定。
- 写 `ethercat_driver_ros2` 的 `GenericEcSlave` YAML：关节从站映射 `0x607A`（position 命令）、`0x700A`（effort 命令）、`0x6060`、`0x6040`，状态 `0x20A0`/`0x6069`/`0x3B69`/`0x6041`/`0x6061`/`0x700B`；手从站单独一个。
- 安全逻辑：看门狗、软限位、速度限幅、`0x3B69` 超限停机、`0x700B` 非零停机、急停输入。

产出：单关节在 ros2_control 下 CSP 位置跟踪与 CSJT 力矩跟踪均可用，扭矩传感器零点标定完成，安全停机可触发。

### 阶段 4：整臂位置模式 + OCS2

- 七个从站成链，`GenericEcSlave` 扩到 7 轴；HOME、低速关节轨迹、MoveIt2 直线各验证一次（CSP）。
- 接 `ocs2_arm_controller`：OCS2 输出关节速度积分成位置写入 CSP 目标；MPC 线程与 1 kHz 写周期分离。
- 扩到双臂（两个 master），记录求解时间与总线周期抖动。
- 手从站：`ENABLE_SET`、`ANGLESET`、`FORCESET` 跑通，`FORCEACT` 读回。

产出：双臂实机由 OCS2 在线跟踪 RViz 目标；手可按力阈值抓握。

### 阶段 5：重力模型与 CSP 导纳（过渡，用 FZI）

- 重力模型：CSP 下摆 20–30 个静止姿态，记录 `0x3B69` 与关节角，用 Pinocchio 重力回归矩阵最小二乘解各连杆质量-质心（含手）。
- 外力矩 \(\tau_{ext} = 0x3B69 - g(q)\)，经 \(J^{+T}\) 发布为末端 wrench。
- 用 FZI `cartesian_compliance_controller`（恒力贴合、柔顺）和 `cartesian_force_controller`（单轴力跟踪）。`command_interfaces` 选 position，写入 `0x607A`。末端帧用 `*_hand_tcp`，`ft_sensor_ref_link` 用发布 wrench 的那一帧。七轴冗余用其零空间姿态项收肘。
- OCS2 的末端参考作为 FZI 的目标位姿。单轴恒力、慢速拖动、刚性接触三类测试。

产出：重力模型残差可接受；CSP 下接触面维持设定力，自由空间由 OCS2 避碰、FZI 执行。此路线同时作为 CSJT 不可用时的兜底。

### 阶段 6：CSJT 整臂力矩控制（目标方案）

- 整臂切 −11 前提：阶段 5 重力模型完成、抱闸行为已确认、`0x7007` 限幅带已设。
- 整臂重力补偿悬停：\(\tau_{0x700A} = g(q)\)，逐关节加入，先 J7→J1 顺序。确认 `0x2202` 语义后决定是否把 \(g(q)\) 挪给伺服。
- 接 Cartesian-Impedance-Controller：笛卡尔刚度阻尼 + 零空间关节阻抗收肘，OCS2 发参考位姿；按需用 SDO `0x7011/0x7012` 调扭矩环带宽与阻尼比改拖动手感。
- 动力学辨识（惯量、科氏、摩擦）在需要高带宽或快速运动时再做，用 `0x2201` 下发低速侧惯量。
- 可选：读 `qm_door` force-tracking 分支，把末端力跟踪写成 OCS2 的代价或约束项。

产出：整臂零力拖动、笛卡尔阻抗接触；与阶段 5 对比接触刚度、响应、稳定性，确定交付方案。

## 6. 交付口径

- 能做到：自建 EtherCAT 主站下的 OCS2 实时 MPC，带限位、自碰撞、双臂避碰；CSJT（−11）关节扭矩闭环下的重力补偿、零力拖动、笛卡尔阻抗；CSP + 导纳作为过渡与兜底。
- 需要确认：抱闸控制与时序、`0x2201/0x2202` 语义、−11 切换条件、`0x700B` 错误位表。抱闸未确认前不在 J2–J4 上切 −11。
- 风险：无厂家碰撞检测与安全控制器，安全层自建；CSJT 下 `0x700A = 0` 即关节松弛，重力模型错误直接表现为坠落或漂移；DC 同步与 PREEMPT_RT 配置不当会导致总线丢帧；触觉 PDO 的 SM 分配需验证。
