FROM ubuntu:24.04
RUN apt-get update && apt-get install -y git curl
RUN useradd -m user
USER user
RUN curl -fsSL https://claude.ai/install.sh | bash

# Symlink ~/.claude.json into the mounted credentials volume so that
# changes made during interactive login persist across containers.
RUN ln -sf /home/user/.claude/.claude.json /home/user/.claude.json
