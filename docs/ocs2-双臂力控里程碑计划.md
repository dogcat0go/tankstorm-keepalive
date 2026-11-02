# BR-R73R 双臂 OCS2 力控：里程碑计划

- 对象：中科新松 BR-R73R 双七轴臂，URDF `art7by_arm`，可控关节 `left_joint1`–`7`、`right_joint1`–`7`，共 14 个。
- 已有基础：双轮 VMC、LQR、PID、减速电机三环 PI、MoveIt2 笛卡尔规划。
- 目标：在现有双臂上加入 OCS2 实时 MPC，并把力传感器反馈接入控制回路。

## 1. 硬件边界与方案形态

控制柜（DucoCore）对外开放的接口决定了方案形态：

| 接口 | 能否用 | 说明 |
| --- | --- | --- |
| `servoj` 关节位置流 | 能 | OCS2 的唯一输出通道，非阻塞，每周期 7 个关节角（rad） |
| `fc_config` / `fc_start` / `fc_stop` / `fc_guard_act` | 能 | 柜内末端力控，按轴选择柔顺方向、目标力、阻尼、速度上限、死区，可与 `servoj` 叠加 |
| `get_tcp_force` / `get_ext_torque` | 能 | 末端六维力、关节观测外力矩，作为外环反馈 |
| 关节力矩指令 | 不能 | SDK 中标记为预留，第三方不可用 |

因此可实现的形态是：

- OCS2 做带关节限位、自碰撞、双臂避碰的实时 MPC，输出关节速度，积分为位置后发 `servoj`。
- 力传感器读数进入 OCS2 的末端参考（修改 `ReferenceManager` 目标）或作为约束项。
- 接触刚度由柜内 `fc_*` 或 OCS2 下游的导纳层保证。
- 力矩级笛卡尔阻抗（Franka / iiwa 那类）在这台臂上做不到。

## 2. URDF 要点

- 腰、颈、腿、轮子、手指全部为 `fixed`，OCS2 以 `base_link` 为固定基座，只优化 14 个转动关节。
- 关节限位与样本一致：J1/J3/J5 ±170°，J2 ±100°，J4 −65°～+155°，J6 ±90°，J7 −75°～+90°。
- 力矩上限：J1/J2 100 N·m，J3/J4 50 N·m，J5–J7 20 N·m。
- J6→J7 沿轴向偏 75 mm，腕为异面十字，使用数值逆解。
- `left_joint7` 的 `rpy` 带约 0.5° 导出残差，保持原样，不要改成整齐的 π/2。
- 左右 J2 轴向相反，两臂不能共用同一套关节符号。
- 连杆惯量仅供可视化，不能当作力矩控制的动力学模型。
- 碰撞网格为完整 STL，接 OCS2 自碰撞前先换成圆柱和盒子。
- `*_force_sensor` 只是网格，无传感器坐标系，力数据只能从控制器读。
- 末端帧：抓持用 `left_hand_tcp` / `right_hand_tcp`，对指用 `*_hand_pinch_tcp`，只控法兰用 `left_arm_link_7` / `right_arm_link_7`。

## 3. 参考项目

### OCS2 本体

- [leggedrobotics/ocs2](https://github.com/leggedrobotics/ocs2)，文档：[Getting Started](https://leggedrobotics.github.io/ocs2/getting-started.html)、[Optimal Control Modules](https://leggedrobotics.github.io/ocs2/optimal_control_modules.html)、[Robotic Examples](https://leggedrobotics.github.io/ocs2/robotic_examples.html)。重点读 `ocs2_mobile_manipulator` 的 `task.info`、`MobileManipulatorInterface`、`EndEffectorConstraint`、`JointVelocityLimits`、`SelfCollisionConstraint`、`MPC_MRT_Interface`。
- [legubiao/ocs2_ros2](https://github.com/legubiao/ocs2_ros2)：ROS 2 Humble/Jazzy 可编译，自带 dual-arm mobile manipulator 例子，双臂 URDF 最终替换到这里。

### OCS2 接真机的模板

- [YangLuo-Bionics/arms_ros2_control](https://github.com/YangLuo-Bionics/arms_ros2_control)：`ocs2_arm_controller` 是 ros2_control 控制器（HOLD/HOME/OCS2 状态机、RViz 目标、位置输出）；`hardwares/` 下的 topic_based 与各厂商硬件接口是给 DUCO 写硬件接口的模板。
- [qiayuanl/legged_control](https://github.com/qiayuanl/legged_control)：OCS2 + ros_control 的经典接法，MPC 线程与实时线程分离，重点看 `LeggedController`。

### 力控层

- [ros2_controllers admittance_controller](https://github.com/ros-controls/ros2_controllers/tree/master/admittance_controller)：位置接口机器人的标准导纳，F/T 语义组件、重力补偿、导纳积分、微分 IK。
- [FZI cartesian_controllers](https://github.com/fzi-forschungszentrum-informatik/cartesian_controllers)：`cartesian_compliance_controller`、`cartesian_force_controller`，面向位置/速度接口，可直接作为 OCS2 下游的柔顺层。
- [yifan-hou/force_control](https://github.com/yifan-hou/force_control)：混合力/速度控制，非 ROS，用于理解选择矩阵与轴向切换。
- [matthias-mayr/Cartesian-Impedance-Controller](https://github.com/matthias-mayr/Cartesian-Impedance-Controller)：只读，理解力矩接口下阻抗为何是首选，以及这台臂为何不走这条路。

### 力进 OCS2 优化问题

- [skywoodsz/qm_control](https://github.com/skywoodsz/qm_control) `feature-force` 分支、[danisotelo/qm_door](https://github.com/danisotelo/qm_door) `force-tracking` 分支：末端接触力写成 OCS2 的约束/代价，以及与 WBC 的任务分配。固定基座七轴只需其中的末端力跟踪项。

### 厂商侧

- 中科新松 [二次开发文档](https://docs.ducorobots.cn/zh/develop/latest/)、`duco_ros2_driver`、`DucoCobot` C++ SDK。用到的接口：`servoj`、`get_actual_joint`、`get_tcp_force`、`get_ext_torque`、`fc_config` / `fc_start` / `fc_stop` / `fc_guard_act`。
- `duco_ros2_driver` 自带的是 GCR 六轴和 `double_arm_moveit_config`，没有 BR-R73；`/duco_robot/robot_move` 是阻塞式 `movej`/`movel`，不用于 OCS2 实时环。

## 4. 知识衔接

| 已有 | 对应到本项目 |
| --- | --- |
| LQR | OCS2 的 SLQ/iLQR 是沿标称轨迹反复线性化后解时变 LQR，再滚动时域；Q、R 调参直觉直接用于 `task.info` |
| VMC | 导纳控制：力误差经虚拟质量、阻尼、刚度变成位移，对应 `admittance_controller` 的 `M`、`D`、`K` |
| 三环 PI | 电流、速度、位置环都在控制柜内；外部输出是位置目标，`servoj` 的 `kp`、`kd` 只是柜内位置环增益 |
| MoveIt2 | MoveIt2 离线出一条轨迹，OCS2 每周期重解；`EndEffectorConstraint` 相当于笛卡尔目标 |

## 5. 里程碑

按里程碑推进，每个阶段有可运行的结果后再进入下一阶段。

### 阶段 0：补理论

- 最优控制：离散 LQR → iLQR/DDP，时域、终端代价、软硬约束。
- 柔顺控制：阻抗 vs 导纳，位置接口为何只能导纳，接触刚度与稳定性。
- Pinocchio：`forwardKinematics`、`computeJointJacobians`、`getFrameJacobian`。

产出：能说明 OCS2 的状态、输入、代价、约束在七轴臂上各是什么。

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

### 阶段 3：接真机

- 按 `arms_ros2_control` 模板写 `duco_ros2_control` 硬件接口：`read()` 读关节角、`get_tcp_force`、`get_ext_torque`；`write()` 发 `servoj`。
- 先用 topic_based / mock 硬件跑通 `ocs2_arm_controller`，再上单臂真机，速度上限和软限位先放紧。
- 完成关节速度积分成位置、通信丢失保护、`servoj` 7 轴确认。

产出：单臂实机由 OCS2 在线跟踪 RViz 目标，再扩到双臂。

### 阶段 4：加力控

- 先用柜内 `fc_config` / `fc_start` 做一轴恒力，与 OCS2 位置流叠加，确认传感器标定与安全监控（`fc_guard_act`）。
- 读 `admittance_controller` 与 FZI 源码，在 OCS2 下游实现导纳层：力读数 → 位姿修正 → 回写 OCS2 末端参考（`ReferenceManager`）或作为约束。
- 对比柜内力控与导纳层的接触刚度、响应和稳定性，选一种作为交付方案。

产出：接触面上维持设定力，自由空间仍由 OCS2 避碰跟踪，切换无冲击。

### 阶段 5：力进优化问题（可选）

- 读 `qm_door` force-tracking 分支，把末端力跟踪写成 OCS2 的 `StateInputCost` 或约束项，在输入中加入接触方向上的末端速度。
- 仅当阶段 4 的导纳方案不满足任务（例如双臂协同夹持需要内力分配）时再做。

## 6. 交付口径

- 能做到：OCS2 实时 MPC，带限位、自碰撞、双臂避碰；力传感器反馈进入 MPC 参考或约束；接触刚度由柜内力控或导纳层保证。
- 做不到：力矩级阻抗、接近 Franka / iiwa 的零力拖动手感；原因是控制柜不开放关节力矩指令。
- 风险：`servoj` 软实时与滤波带来滞后，刚性环境下导纳参数必须保守；末端六维力需在 DucoCore 内完成负载标定；`fc_stop` 必须等当前运动结束后调用。
