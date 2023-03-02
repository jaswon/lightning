# Copyright The Lightning AI team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from abc import ABC, abstractmethod

from lightning.fabric.strategies.launchers.launcher import _Launcher as _FabricLauncher
from lightning.pytorch.trainer.connectors.signal_connector import _SIGNUM


class _Launcher(_FabricLauncher, ABC):
    @abstractmethod
    def kill(self, signum: _SIGNUM) -> None:
        """Kill existing alive processes."""

    def on_exception(self, exception: BaseException) -> None:
        """Called by the strategy when the trainer execution is interrupted by an exception."""
