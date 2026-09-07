# ML 学習・開発用コンテナ。
#
# base は debian:trixie とする。Python 3.13 と開発ヘッダ (python3.13-dev) を
# 標準 package で持つため、pyproject.toml の python-preference = "only-system" を
# 変えずに済む。開発ヘッダは torch.compile の inductor backend が triton の C 拡張を
# build するのに必要で、これが無いと計画で既定 ON の torch.compile が落ちる。
# Raspberry Pi 5 も Debian Trixie なので、3 環境で Python の出どころがそろう。
#
# CUDA base image は使わない。torch の cu130 wheel が CUDA runtime library を同梱し、
# driver 側の libcuda は NVIDIA Container Toolkit が注入する。
FROM debian:trixie

# 対話 prompt を出さずに apt を通す
ENV DEBIAN_FRONTEND=noninteractive

# host と uid/gid をそろえる。bind mount したリポジトリへ書いた file の所有者が
# host 側で root にならないようにする。
ARG USER_NAME=developer
ARG USER_UID=1000
ARG USER_GID=1000

ARG UV_VERSION=0.11.24
ARG GLAB_VERSION=1.116.0

# - build-essential: inductor と triton の C 拡張 build
# - python3.13-dev: Python.h（同じ理由）
# - git / openssh-client: リポジトリ操作と SSH remote への push
# - sudo: NOPASSWD で package を足せるようにする
#
# git-lfs は入れない。Debian の package が /etc/gitconfig へ filter を登録するのに
# host 側は未設定なので、同じ working tree を bind mount で共有すると LFS pointer の
# 展開状態が食い違う。ML の学習データは LFS 管理外。
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        build-essential \
        ca-certificates \
        curl \
        git \
        less \
        openssh-client \
        pkg-config \
        python3.13 \
        python3.13-dev \
        python3.13-venv \
        sudo \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

# glab は Debian の package に無いので GitLab の release から入れる
RUN arch="$(dpkg --print-architecture)" \
    && curl -fsSL -o /tmp/glab.deb \
        "https://gitlab.com/gitlab-org/cli/-/releases/v${GLAB_VERSION}/downloads/glab_${GLAB_VERSION}_linux_${arch}.deb" \
    && dpkg -i /tmp/glab.deb \
    && rm -f /tmp/glab.deb

# uv は version を固定して入れる。gitlab-runner/setup.sh と同じ方針
RUN curl -fsSL "https://astral.sh/uv/${UV_VERSION}/install.sh" \
        | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh \
    && uv --version

# host と同じ uid/gid の一般ユーザーを作り、sudo を NOPASSWD にする。
# 既に同じ gid / uid を持つ system group / user がいる image でも通るようにする。
# compose 側は /home/${USER_NAME} を mount 先に書くので、既存 user の流用で名前が
# ずれた場合は build を落とす（黙って別 path を作らせない）。
RUN if ! getent group "${USER_GID}" >/dev/null; then \
        groupadd --gid "${USER_GID}" "${USER_NAME}"; \
    fi \
    && if ! getent passwd "${USER_UID}" >/dev/null; then \
        useradd --uid "${USER_UID}" --gid "${USER_GID}" --create-home \
            --shell /bin/bash "${USER_NAME}"; \
    fi \
    && resolved="$(getent passwd "${USER_UID}" | cut -d: -f1)" \
    && if [ "${resolved}" != "${USER_NAME}" ]; then \
        echo "uid ${USER_UID} は既に ${resolved} が使っています。USER_NAME=${USER_NAME} と一致しません" >&2; \
        exit 1; \
    fi \
    && printf '%s ALL=(ALL) NOPASSWD:ALL\n' "${USER_NAME}" \
        > /etc/sudoers.d/"${USER_NAME}" \
    && chmod 0440 /etc/sudoers.d/"${USER_NAME}" \
    && visudo -c

# 仮想環境をリポジトリの外へ置く。bind mount した .venv は host の interpreter を
# 指しているので、共有すると壊れる。
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
# bind mount 越しの hardlink は失敗しうるので copy にする
ENV UV_LINK_MODE=copy
ENV PATH=/opt/venv/bin:$PATH

# torch.compile の生成コード cache を named volume の下へ向ける。既定は
# /tmp 配下なので container を作り直すと毎回 full compile になる。
ENV TORCHINDUCTOR_CACHE_DIR=/home/${USER_NAME}/.cache/torchinductor
ENV TRITON_CACHE_DIR=/home/${USER_NAME}/.triton

# named volume を当てる path は image 側に所有者付きで作る。存在しない mount 点は
# Docker が root 所有の空 volume として作るため、一般ユーザーが書けなくなる。
RUN install -d -o "${USER_UID}" -g "${USER_GID}" \
        /opt/venv \
        /workspace \
        "/home/${USER_NAME}/.cache" \
        "/home/${USER_NAME}/.cache/uv" \
        "/home/${USER_NAME}/.cache/pre-commit" \
        "/home/${USER_NAME}/.cache/torchinductor" \
        "/home/${USER_NAME}/.triton" \
        "/home/${USER_NAME}/.config"

USER ${USER_UID}:${USER_GID}
WORKDIR /workspace

CMD ["bash"]
