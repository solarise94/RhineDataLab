# UI 修复审阅与后续跟进

## 范围

本文记录 `ui` 分支这一轮 UI 修复工作的结果。
本轮主要处理以下高摩擦交互问题：

- 项目创建
- 数据目录挂载
- 文件管理
- 工作台设置
- 导航结构

同时也记录本轮审阅结论，以及当前仍然保留的主要后续 UI 问题。

本次同步也补充记录了后续一轮已上线的中文化与入口统一结果，并把
“下一步修改”更新为当前仍未完成的结构与风格收口项。

## Completed In This Pass

### 1. Project creation and optional data-directory mount

Files:

- `frontend/components/projects/ProjectDashboard.tsx`

Changes:

- Removed the forced 2-second auto-redirect after mount failure.
- Added a recovery warning flow when project creation succeeds but data-directory mount fails.
- Added `createdProjectId` state so the user can still enter the newly created project.
- Closed the creation form after mount failure instead of leaving the user inside a stale form.
- Added explicit warning actions:
  - `进入项目`
  - `关闭`
- Removed the duplicate top toolbar `使用当前目录` button.
- Kept a single confirmation button near the bottom of the directory browser.
- Added empty-directory guidance:
  - `当前目录为空，仍可作为挂载点。`

Result:

- the user is no longer redirected away before reading the error
- the user is no longer trapped in the creation form
- the directory selection confirmation is colocated with browsing

Related commits:

- `ca51525` `fix(ui): 挂载数据目录与文件管理交互优化`
- `e6ae712` `fix(ui): 修正挂载失败流程、Provider编辑语义、目录浏览器重复按钮`
- `1df3ccd` `fix(ui): 修正挂载失败恢复 banner 状态被 resetForm 抹掉的问题`

Note:

- `ca51525` 同时也覆盖了 Section 2 和 Section 4 中的部分修改，不只影响 `ProjectDashboard.tsx`。

### 2. Data-directory settings interaction

Files:

- `frontend/components/settings/SettingsPanels.tsx`

Changes:

- Added confirmation before switching an already mounted data directory.
- Changed cancel wording from:
  - `取消挂载`
  - `取消切换`
  to:
  - `取消`
- Strengthened mounted-state feedback in the section header by showing the mounted path.
- Localized section titles:
  - `Runtime Preferences` -> `运行时偏好`
  - `API Settings` -> `API 设置`
  - `Diagnostics` -> `诊断`

Result:

- switching mount targets now has symmetric protection with unmount
- mounted-state feedback is stronger
- wording is less misleading during selection/cancel flows

Related commits:

- `ca51525` `fix(ui): 挂载数据目录与文件管理交互优化`

### 3. Provider editing clarity

Files:

- `frontend/components/settings/SettingsPanels.tsx`

Changes:

- Renamed provider edit action from `完成` to `关闭编辑`.
- Preserved the explicit reminder:
  - `Provider 配置已更新，请点击下方「保存 API 设置」生效。`

Result:

- provider editing no longer falsely suggests persistence on exit from edit mode

Related commits:

- `e6ae712` `fix(ui): 修正挂载失败流程、Provider编辑语义、目录浏览器重复按钮`

### 4. Files workspace improvements

Files:

- `frontend/components/files/FilesPanel.tsx`

Changes:

- Renamed `Files Workspace` to `文件管理`.
- Added a direct entry point to settings when no data directory is mounted.
- Moved `解除挂载` out of the small header area into the panel body action row.
- Changed file rows so plain files are no longer rendered as click targets with no action.
- Kept directory rows clickable for navigation.

Result:

- the unmounted state now has a clear next step
- dangerous unmount is less cramped and less easy to mis-tap
- file rows no longer look interactive while doing nothing

Related commits:

- `ca51525` `fix(ui): 挂载数据目录与文件管理交互优化`

### 5. Navigation adjustment

Files:

- `frontend/components/layout/SideNav.tsx`

Changes:

- Promoted `工作台设置` into the main `工作台` group.
- Removed it from the `高级` group.

Result:

- frequently used configuration is no longer hidden under an advanced-only mental model
- project configuration is now grouped with the rest of the daily workbench flow
- `技术详情` remains the only item in `高级`, which better matches its lower-frequency and more technical nature

Related commits:

- `ca51525` `fix(ui): 挂载数据目录与文件管理交互优化`

### 6. Cross-page title and language unification sync

Files:

- `frontend/components/layout/ProjectWorkspace.tsx`
- `frontend/components/projects/ProjectDashboard.tsx`
- `frontend/components/results/ResultsGrid.tsx`
- `frontend/components/results/ResultsOverviewChart.tsx`
- `frontend/components/files/FilesPanel.tsx`
- `frontend/components/report/ReportBuilder.tsx`
- `frontend/components/settings/SettingsPanels.tsx`
- `frontend/components/advanced/AdvancedPanels.tsx`
- `frontend/components/layout/SideNav.tsx`
- `frontend/components/layout/ProjectHeader.tsx`
- `frontend/components/cards/CardStatusBadge.tsx`

Changes:

- Unified page-level titles into Chinese across the major workspace modules:
  - `Projects` -> `项目管理`
  - `Accepted and candidate results` -> `结果库`
  - `Uploads, data assets, and execution files` -> `文件管理`
  - `Report assembly` -> `报告`
  - `Runtime, libraries, and API settings` -> `工作台设置`
  - `Graph and Git history` -> `技术详情`
- Localized the results surface:
  - `Results Overview` -> `结果概览`
  - `Status distribution` -> `状态分布`
  - `Accepted / Candidate / Other` -> `已接受 / 候选 / 其他`
  - result bucket titles localized
- Localized the files surface:
  - `tracked files` -> `跟踪文件`
  - `Data Assets` / `Session Uploads` / `Execution Files` localized in the visible UI
- Localized the report surface:
  - `Report Builder` -> `报告`
  - `sections` -> `个章节`
- Localized the settings and advanced surfaces:
  - runtime / provider / library / diagnostics labels normalized into Chinese
  - `Git History` -> `Git 历史`
- Localized shared status and navigation vocabulary:
  - `Sessions` -> `会话`
  - `active` -> `活跃`
  - project placeholder copy normalized to `示例：RNA-seq 项目`

Result:

- the main navigation language and the destination page language are now aligned
- the workspace no longer immediately switches back into older English headers
- the product entry path reads more like one coherent application instead of mixed-era UI

Traceability note:

- these source changes were verified in the `ui` branch working tree during this document sync
- at the time of this sync they were deployed by the operator, but not yet recorded here as a dedicated commit hash

## Review Notes From This Pass

### Fixed review findings

The following review findings were raised and then fixed on the `ui` branch:

1. Project creation mount failure left no safe exit into the newly created project.
2. Provider edit action label still implied persistence.
3. Project creation directory browser had duplicate `使用当前目录` buttons.
4. Mount-failure recovery banner state had incomplete cleanup and could leave stale warning/error state behind.
   - affected state:
     - `createdProjectId`
     - `formError`
   - failure mode:
     - `resetForm()` cleared recovery state too early
     - later cleanup only partially cleared banner state, allowing stale warning/error UI
   - fix landed in:
     - `1df3ccd` `fix(ui): 修正挂载失败恢复 banner 状态被 resetForm 抹掉的问题`

## Remaining Follow-Up

### Advanced / 技术详情 layout still feels visually cut

Files involved:

- `frontend/components/advanced/AdvancedPanels.tsx`
- `frontend/components/detail/CardDetailPanel.tsx`
- `frontend/components/layout/ProjectWorkspace.tsx`
- `frontend/app/globals.css`

Current issue:

- In the `技术详情` page, the area below `Git History` feels like a hard white-box truncation.
- This is caused by page layout, not by the `Git History` list itself.

Root cause:

- `advanced` view is vertically stacked into:
  - an upper `AdvancedPanels` block
  - a lower `CardDetailPanel`
- the page height is constrained and uses hidden overflow
- the upper panel is treated as non-shrinking content
- the lower panel only occupies the remaining height

Relevant layout rules:

- `frontend/app/globals.css`
  - `.advanced-content`
  - `.advanced-view`
  - `.advanced-view > .panel:first-child`
  - `.card-detail-panel-shell`

Observed result:

- the lower detail card appears visually cut off beneath the final `Git History` section
- the page reads like two unrelated stacked white blocks instead of one coherent technical detail surface

Recommended next step:

Choose one of these directions:

1. make the advanced page a normal vertically scrolling page
2. split advanced content into a left/right layout instead of upper/lower stacking

Do not treat this as a `Git History` card styling problem alone. The primary issue is the advanced page height/overflow structure.

### Cross-page entry and style consistency still needs a dedicated pass

This remediation pass fixed several concrete interaction issues, but the
workspace still has a broader consistency problem across:

- 入口项目管理页
- 结果库
- 文件管理
- 报告
- 工作台设置

#### 1. First-screen structure is still not unified across major modules

Current problem:

- Files starts with a management / upload surface.
- Results starts with an overview chart and then multiple grids.
- Report starts with a builder card.
- Settings starts directly with configuration sections.
- Project dashboard uses yet another entry pattern with a separate page hero and create flow.

Result:

- the main workbench modules still do not feel like sibling pages
- users still have to relearn where the primary action area lives on each page

Recommended next step:

- choose one shared page skeleton for:
  - page intro
  - top summary / status row
  - primary action or primary dataset panel
  - secondary panels below
- apply that skeleton consistently to results, files, report, settings, and the project dashboard root page

#### 2. Project dashboard still feels older than the downstream workspace pages

Current problem:

- The root title is already localized to `项目管理`, but the create flow still contains older operator vocabulary such as `Project ID`.
- The page copy still mixes Chinese with product-internal nouns such as `Sessions` and `Cards`.
- The header rhythm and form presentation still feel more utilitarian than the newer workspace modules.

Result:

- first impression is improved, but the entry page still sets a slightly different tone from the rest of the workspace
- the project-creation surface is not yet visually aligned with results / files / report / settings

Recommended next step:

- localize the remaining operator-facing copy where possible
- rename or annotate `Project ID` in a more user-facing way
- bring header spacing, type scale, and form section styling into the same system used by the internal workbench pages

#### 3. Files page still has copy and taxonomy drift

Current problem:

- visible headings are now localized, but some copy still leaks older mixed terminology:
  - `session uploads` still appears in explanatory text
  - execution file category labels remain English:
    - `Task Packet`
    - `Manifest`
    - `Dependency Issue`
    - `Review Context`
    - `Reviewer Trace`
    - `Transcript`
    - `Agent Trace`
    - `Agent Output Timeline`
    - `Generated Script`
- upload entry and upload-result grouping are clearer than before, but still feel like separate sub-products inside one page

Result:

- files is no longer blocked by navigation friction, but vocabulary and information grouping are not fully settled
- user-facing terminology still mixes end-user language with internal execution artifacts

Recommended next step:

- finish the remaining Chinese terminology pass in files
- decide which execution artifact names should stay technical and which should get user-facing labels
- consider moving upload result feedback closer to the upload action area if that page gets a broader first-screen restructure

#### 4. Settings and advanced surfaces still expose internal/operator vocabulary

Current problem:

- major section headers are localized, but some technical wording still leaks directly into the user-facing UI:
  - `system`
  - `Provider`
  - `CLI`
  - `Manifest`
- this is most visible in runtime/help text and advanced diagnostics copy

Result:

- settings is much more coherent than before, but still reads partly like an operator console
- advanced is localized at the header level but not yet fully normalized in its supporting language

Recommended next step:

- separate user-facing wording from backend/deploy terminology where possible
- keep unavoidable technical words, but wrap them in clearer explanatory copy
- normalize default-runtime wording so `__system__` / `system` read as product language rather than raw implementation language

#### 5. Visual primitives and typography are still fragmented

Current problem:

- Results cards rely on one set of inline layout primitives.
- Report cards use another.
- Settings uses a more stable `settings-*` component family.
- Project dashboard still has a separate header/form visual rhythm.
- Global typography is still anchored to the older default sans stack rather than a more intentional product-wide hierarchy.

Result:

- spacing, density, and hierarchy still drift between pages
- the product now speaks one language more consistently, but does not yet look like one fully unified design system

Recommended next step:

- extract shared panel / card header / meta / action-row primitives where possible
- reduce page-local inline style islands in results, files, and report
- define one consistent type scale and header rhythm for:
  - page title
  - page subtitle
  - panel title
  - meta/status text

#### 6. Advanced / 技术详情 layout still needs structural repair after the language pass

Current problem:

- the header and section naming are now localized
- the lower detail area still feels visually cut off beneath the upper diagnostics block
- this remains a layout problem, not a language problem

Result:

- localization improved comprehension, but the stacked white-box truncation still breaks visual continuity
- the page still reads like two unrelated surfaces instead of one coherent technical-detail workspace

Recommended next step:

- make the advanced page a normal vertically scrolling page, or split it into a left/right layout
- do not treat this as a `Git 历史` card styling issue in isolation
- repair the page-level height and overflow structure first

### Recommended sequencing for the next UI pass

Suggested order:

1. repair the `技术详情` page layout so the stacked white-box cutoff is gone
2. standardize the first-screen structure across project dashboard, results, files, report, and settings
3. finish the remaining vocabulary cleanup in project dashboard, files, settings, and advanced
4. consolidate visual primitives, spacing, and typography into reusable styling rules

Rationale:

- Step 1 fixes the most visibly broken layout issue that still makes the UI feel unfinished even after the language pass.
- Step 2 should come next because shared page skeletons will determine where later visual refinements should attach.
- Step 3 is now a focused cleanup pass rather than a broad translation pass, and should follow the structural decisions.
- Step 4 is the most reusable design-system work and will benefit from the page skeleton and vocabulary being settled first.

## Verification

Verified during this pass:

- `cd frontend && npm run build`
- source-level verification of the later localization/title updates in the `ui` branch working tree during this document sync

What that build verification covered:

- Next.js production build
- build pipeline内的校验步骤
- TypeScript compilation checks included in the production build flow

Manual/browser verification:

- source-level review was performed for the modified pages and flows
- deployment/runtime verification for the later localization pass was reported by the operator, not independently re-run during this doc-only sync
- no separate browser-driven walkthrough was recorded in this document

Known unverified areas:

- no dedicated browser replay of the mount-failure recovery flow is recorded here
- no visual regression suite or screenshot-based comparison was run
- this document sync did not re-run `npm run build`; it only synchronized the recorded status with the already deployed UI changes

Status:

- build passed after the UI changes reviewed in this document
