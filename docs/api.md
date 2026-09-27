# 服务接口

所有时间使用带时区的 ISO 8601 格式。服务持久化 UTC 时间，按诊所配置的时区解释运营日期。JSON 请求大小上限为 1 MB；无效请求返回稳定的错误码和 HTTP 状态，不向调用方透出数据库异常。

## 登录与诊所隔离

`POST /auth/token` 接受 `staff_id` 和 `password`，返回有效期不超过一天的 Bearer 凭据。除登录与健康检查外，请求必须同时提供 `Authorization: Bearer …` 和 `X-Clinic-ID`。认证失败不区分账号不存在、停用或密码错误；诊所边界之外的数据返回不存在，避免泄露另一诊所的记录。

`POST /auth/logout` 撤销当前凭据。修改员工密码会撤销该员工的全部活动凭据。初始负责人通过命令行创建；没有可直接注册负责人的 HTTP 路由。

## 患者、评估与诊疗计划

- `POST /patients` 建立诊所内患者档案；外部编号在诊所范围内唯一。
- `GET /patients/{patient_id}` 返回最小档案，不返回联系方式密文。
- `POST /patients/{patient_id}/merge` 以两个版本号和书面原因将重复档案标记为合并，并指向保留档案。
- `POST /patients/{patient_id}/assessments` 新建评估草稿；`POST /assessments/{assessment_id}/sign` 由临床岗位签署。
- `POST /patients/{patient_id}/consents` 创建更高版本的授权；`POST /consents/{consent_id}/withdraw` 撤回授权。
- `POST /patients/{patient_id}/plans` 建立计划，医美和体重管理计划必须引用当前对应授权。
- `POST /plans/{plan_id}/{propose|activate|pause|resume|complete|cancel}` 以 `expected_version` 执行带版本保护的状态转换。
- `GET /patients/{patient_id}/weight-series` 返回按观察时间排序的测量值，不生成诊断或治疗建议。

评估签署后不可覆盖。就诊病历由章节组成，签署需要主诉、评估和计划三部分；签署后的补充内容成为新版本，原始文字仍保留。

## 预约、随访与计划节点

创建预约须提供 `Idempotency-Key`，有责任人的预约不能与未结束时段重叠。临时占位到期后由 `POST /appointments/{id}/book` 拒绝确认，过期占位可通过服务方法按限额释放。预约状态按占位、确认、到诊、服务、完成推进；开始服务时产生就诊记录。

随访和计划节点支持领取租约、版本校验、幂等创建、延期和完整处置历史。旧领取者不能以过期令牌提交结果；重新领取不会删除前次领取事件。

## 诊所耗材

- `POST /products` 登记耗材；`POST /products/{product_id}/lots` 按批号入库。
- `POST /stock/reserve` 依据失效日期按先到期先出分批预留，需要 `Idempotency-Key`。
- `POST /stock/{reservation_id}/consume` 记录患者使用；`release` 释放尚未使用的数量。
- `POST /stock/{lot_id}/quarantine`、`recall` 或 `release-quarantine` 记录批次处置及受影响预留。
- `GET /stock/lots` 查看可用数量；`GET /stock/{lot_id}/history` 查看批次流水。

入库、占用、释放与患者使用均进入不可变流水。存在不足时整笔预留回滚；被隔离、召回或在诊所本地日期已过期的批次不能继续使用。

## 批次召回处置

`POST /stock/{lot_id}/recalls` 依据供应商通知开放召回，需要 `Idempotency-Key` 与通知编号、通知时间、紧急程度（`low`/`moderate`/`high`/`urgent`）和摘要。开放时批次被置为召回状态，并**严格按该批号**快照所有 `reserved` 与 `consumed` 的预约预留、核销流水与患者资料；同产品其他批号不会混入。召回前已核销（已领用或正在处置）的患者同样进入病例清单，并保留核销时间、核销人和原始记录。

- 同一供应商通知重复导入：内容一致时回放原召回，不重复建单；内容不一致时拒绝，须走修订接口。
- `POST /recalls/{recall_id}/revisions` 登记供应商修订（修订编号须唯一，支持幂等）。修订只追加并更新摘要/指引，紧急程度只能升级；**任何修订都不会改写或删除已完成的病例处置**。
- `POST /recalls/{recall_id}/escalate` 由内部升级紧急程度，须提交不少于 5 个字的书面升级依据和版本号；只允许向更高等级升级，每次升级在召回事件流中保留前后等级与依据。
- `GET /recalls/{recall_id}` 是负责人视图：批次入库总量、当前结存、已核销/在预留数量、病例总数、未处置人数、待人工复核数，以及每例的责任人、处置阶段（`identified`/`contacted`/`completed`）、联系结果、最后联系时间、后续复核日期、完整快照和不可变事件流。
- 病例操作：`POST /recall-cases/{case_id}/assign` 指定责任人；`POST /recall-cases/{case_id}/contact` 登记联系结果（`reached`/`unreachable`/`declined`，成功联络须记录沟通情况）和后续复核日期；`POST /recall-cases/{case_id}/manual-review` 由临床岗位或负责人完成人工复核；`POST /recall-cases/{case_id}/complete` 完成处置，必须已有后续复核日期，且已实际使用该批号的患者必须成功联络（仅未使用的预约预留可在书面说明下标记 `no_contact_required`）。
- `GET /recalls/{recall_id}/contact-queue` 供普通排班/联络人员使用，仅返回完成联络所需的最小信息（病例编号、紧急程度、阶段、联系状态、复核日期、患者姓名与联系方式密文、预约时段），不返回批号快照、核销明细或汇总数量。排班人员不能查看负责人详情或发起召回。

召回开放后该批号默认拒绝核销；临床岗位可在提交 `recall_acknowledgement` 书面确认后完成核销，但该病例会**自动进入人工复核队列**：若病例此前已被当作"未使用预约"完成，处置会被重新打开而不是从清单消失。待人工复核项和逾期复核日期由 `GET /audit/diagnostics` 报告（`recall.consumption_manual_review`、`recall.review_overdue`）。召回的开放、修订、升级、病例处置全部进入审计哈希链。

## 不良事件与数据使用

护理人员可报告事件或患者安全关注项；临床岗位复核并记录处置，诊所负责人可作废就诊记录。`GET /audit/verify` 校验诊所哈希链，`GET /audit/diagnostics` 汇报需人工核对的一致性问题，不自动修改业务状态。

`POST /patients/{patient_id}/export` 只在存在有效数据导出授权时返回明确选择的章节。导出字段采用白名单，联系方式密文、凭据和内部合并字段不会导出；相同幂等请求得到相同内容摘要。`GET /reports/daily`、`appointments`、`incidents` 和 `overdue-milestones` 仅返回运营汇总或经岗位授权的工作队列。

## 主要状态

- 计划：草稿 → 提议 → 生效；可暂停和恢复，完成或取消后不能重新激活。
- 预约：占位 → 确认 → 到诊 → 服务中 → 完成；取消和未到诊是独立终态。
- 不良事件：已报告 → 分诊 → 观察 → 已解决 → 关闭。每次处置单独记录操作人和理由。
- 耗材预留：预留 → 释放或核销。库存数量由收货、预留、释放和更正流水求和，不直接改写历史数量。
- 召回病例：已识别 → 已联络 → 已完成。召回期间核销的病例先进入人工复核，复核通过后才能完成；紧急程度只升不降，升级与修订均留痕。
