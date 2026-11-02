# BR-R73R 双臂 OCS2 力控：里程碑计划

- 对象：中科新松 BR-R73R 双七轴裸臂（无 DucoCore 控制柜），关节为 EtherCAT 从站，末端为因时 RH56F1 灵巧手。URDF `art7by_arm`，可控关节 `left_joint1`–`7`、`right_joint1`–`7`，共 14 个。
- 已有基础：双轮 VMC、LQR、PID、减速电机三环 PI、MoveIt2 笛卡尔规划。
- 目标：自建 EtherCAT 主站，在双臂上加入 OCS2 实时 MPC，并把关节扭矩传感器反馈接入控制回路。

## 1. 硬件边界与方案形态

没有控制柜，`servoj`、`fc_*`、`get_tcp_force` 这类 DucoCore SDK 接口都不存在。我们自己是 EtherCAT 主站，关节从站对外开放的过程数据决定了方案形态。

### 1.1 关节从站 `SERVO_ECAT_DR3`（ESI `SERVO_ECAT_DR3_0.1.8.xml`）

- Vendor `0x8CF`（SIASUN），Product `0x39252`，Rev 8，Device type `0x00020192`（CiA402）。
- CoE：SDO Info、Segmented SDO、Complete Access；FoE。支持 DC 同步（`AssignActivate 0x300`），周期由主站设为 1 ms。
- PDO 映射 `Fixed`，输出 42 字节、输入 40 字节。ESI 字典里 `0x1600/0x1A00` 的默认子项是旧的，以 `RxPdo/TxPdo` 元素为准。

主站 → 关节（RxPDO `0x1600`）：

| 对象 | 名称 | 含义 |
| --- | --- | --- |
| `0x6040` / `0x6060` | Control word / Modes of operation | 标准 CiA402，模式可按周期切换 |
| `0x607A` / `0x60FF` / `0x6071` | Target position / velocity / torque | CSP、CSV、CST 三套目标均已映射 |
| `0x700A` | Target joint torque（DINT） | 厂商对象：关节输出端力矩指令，与 `0x6071` 电机侧力矩是两条路 |
| `0x60B2` | Torque offset | 力矩前馈 |
| `0x2201` / `0x2202` | JointInertia / GravityOffset | 主站每周期下发该关节的惯量项与重力项，从站内部补偿 |
| `0x3B6D` | Torque sensor offset | 扭矩传感器零点 |
| `0x7007` / `0x7008` | TorqueBand / CurrentBand | 力矩、电流限幅带 |
| `0x60FE` | Digital outputs | 预计含抱闸释放位 |
| `0x20A4` | SafetyBoardState | 臂内安全板状态/指令 |

关节 → 主站（TxPDO `0x1A00`）：

| 对象 | 名称 |
| --- | --- |
| `0x6064` / `0x20A0` | 电机侧位置 / 输出端编码器位置 |
| `0x606C` / `0x6069` | 速度（两路） |
| `0x6077` / `0x6078` | 电机力矩 / 电机电流 |
| `0x3B69` | Torque sensor（DINT，关节扭矩传感器） |
| `0x60F4` | 跟随误差 |
| `0x6041` / `0x6061` / `0x700B` / `0x60FD` | 状态字 / 模式回显 / 错误码 / 数字输入 |

ESI 中缺失、需上线后用 SDO 读取或向厂家确认的项：

- `0x6060` 接受的模式值（字典无 `0x6502`）：`ethercat upload -p<n> 0x6502 0`；并用 `ethercat sdos -p<n>` 导出完整对象字典。
- `0x3B69`、`0x700A`、`0x6071` 的单位与标定系数；`0x6064`/`0x20A0` 的分辨率（`0x608F` 默认为 0，在线读）。
- `0x60FE` 抱闸位定义、`0x20A4` 取值表、使能与抱闸时序。
- `0x7007` / `0x7008` 限幅带单位。
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
- 力控两条路线，由 `0x6060` 实际支持的模式决定：
  1. CSP（`0x607A`）+ 导纳：`0x3B69` 做外力矩输入，主站 1 kHz 积分出位置。先走这条。
  2. 关节力矩模式（`0x700A` 或 CST `0x6071`）+ 笛卡尔阻抗：主站用 Pinocchio 算重力项发 `0x2202`，Cartesian-Impedance-Controller 这类力矩阻抗直接可用，OCS2 发参考位姿。需要重新辨识动力学。
- 安全层全部自建：无碰撞检测、无安全控制器。`0x20A4`、`0x60FE`、`0x7007/0x7008` 的行为先在单关节上确认。

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

- 装 IgH 主站（PREEMPT_RT 内核），`ethercat slaves`、`ethercat sdos`、`ethercat upload 0x6502` 导出模式表与完整对象字典，确认 ESI 缺失项。
- 用 SOEM 或 IgH 示例写最小程序：单关节上电、402 使能、抱闸释放（`0x60FE`）、读 `0x6064/0x20A0/0x3B69`，确定单位与标定系数。
- CSP 模式下单关节小幅正弦跟踪，记录 `0x60F4` 跟随误差与 `0x3B69` 零点漂移；确认 `0x20A4`、`0x7007/0x7008` 行为。
- 写 `ethercat_driver_ros2` 的 `GenericEcSlave` YAML：关节从站（CSP 先映射 `0x607A` 为 position 命令，`0x6064`/`0x606C`/`0x3B69` 为 position/velocity/effort 状态）、手从站。
- 安全逻辑：看门狗、软限位、速度限幅、`0x3B69` 超限停机、急停输入。

产出：单关节在 ros2_control 下按位置跟踪，扭矩传感器读数标定完成，安全停机可触发。

### 阶段 4：整臂位置模式 + OCS2

- 七个从站成链，`GenericEcSlave` 扩到 7 轴；HOME、低速关节轨迹、MoveIt2 直线各验证一次。
- 接 `ocs2_arm_controller`：OCS2 输出关节速度积分成位置写入 CSP 目标；MPC 线程与 1 kHz 写周期分离。
- 扩到双臂（两条链或一条链），记录求解时间与总线周期抖动。
- 手从站：`ENABLE_SET`、`ANGLESET`、`FORCESET` 跑通，`FORCEACT` 读回。

产出：双臂实机由 OCS2 在线跟踪 RViz 目标；手可按力阈值抓握。

### 阶段 5：力控路线 1（CSP + 导纳）

- `0x3B69` 经零点、重力补偿（Pinocchio 重力项）后得到外力矩，映射到末端外力。
- 读 `admittance_controller` 与 FZI 源码，在 OCS2 下游实现导纳层：外力 → 位姿修正 → 回写 OCS2 末端参考（`ReferenceManager`）。
- 单轴恒力、零力拖动、刚性接触三类测试，调 M/D/K 与滤波带宽。

产出：接触面上维持设定力，自由空间仍由 OCS2 避碰跟踪，切换无冲击。

### 阶段 6：力控路线 2（关节力矩模式 + 阻抗）（视 `0x6060` 模式表与任务需求决定）

- 动力学辨识：激励轨迹、最小参数集、摩擦模型；更新 URDF 惯量。
- 单关节力矩模式验证：`0x700A` 或 CST `0x6071`，主站发 `0x2202` 重力项，确认闭环方向与单位。
- 整臂重力补偿悬停，再接 Cartesian-Impedance-Controller（零空间关节阻抗收肘），OCS2 发参考位姿。
- 可选：读 `qm_door` force-tracking 分支，把末端力跟踪写成 OCS2 的代价或约束项。

产出：整臂零力拖动与笛卡尔阻抗接触；与路线 1 对比接触刚度、响应、稳定性，确定交付方案。

## 6. 交付口径

- 能做到：自建 EtherCAT 主站下的 OCS2 实时 MPC，带限位、自碰撞、双臂避碰；关节扭矩传感器反馈进入导纳层或 MPC 参考；若模式表支持关节力矩模式，可做力矩级笛卡尔阻抗与零力拖动。
- 需要确认：`0x6060` 模式表、各力矩对象单位、抱闸与安全板位定义、使能时序。未确认前只能按 CSP 路线推进。
- 风险：无厂家碰撞检测与安全控制器，安全层自建；力矩路线依赖动力学辨识质量；DC 同步与 PREEMPT_RT 配置不当会导致总线丢帧；触觉 PDO 的 SM 分配需验证。
