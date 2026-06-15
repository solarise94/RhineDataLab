#!/usr/bin/env bash
# Mirror preset expansion table.
# Sourced by install_blueprint_re.sh to populate *_mirror environment variables
# from BLUEPRINT_MIRROR_PRESET.

# tsinghua preset
tsinghua_conda_base_url="https://mirrors.tuna.tsinghua.edu.cn/anaconda"
tsinghua_cran_mirror="https://mirrors.tuna.tsinghua.edu.cn/CRAN"
tsinghua_bioconductor_mirror="https://mirrors.tuna.tsinghua.edu.cn/bioconductor"
tsinghua_pypi_mirror="https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple"

# ustc preset
ustc_conda_base_url="https://mirrors.ustc.edu.cn/anaconda"
ustc_cran_mirror="https://mirrors.ustc.edu.cn/CRAN"
ustc_bioconductor_mirror=""
ustc_pypi_mirror="https://pypi.mirrors.ustc.edu.cn/simple"

# default preset (official sources; empty bioconductor means BiocManager default)
default_conda_base_url="https://repo.anaconda.com"
default_cran_mirror="https://cloud.r-project.org"
default_bioconductor_mirror=""
default_pypi_mirror="https://pypi.org/simple"
