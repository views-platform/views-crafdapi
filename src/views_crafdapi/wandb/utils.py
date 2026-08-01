from typing import Union
from statistics import mean
import re
from dataclasses import asdict
import wandb
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

def wandb_alert(
    title: str,
    text: str = "",
    level: wandb.AlertLevel = wandb.AlertLevel.INFO,
    wandb_notifications: bool = True,
    models_path: Union[Path, str] = None
) -> None:
    """
    Sends an alert to Weights and Biases (WandB) if WandB notifications are enabled and a WandB run is active.

    Args:
        title (str): The title of the alert.
        text (str, optional): The text content of the alert. Defaults to an empty string.
        level (wandb.AlertLevel, optional): The level of the alert. Defaults to wandb.AlertLevel.INFO.

    Returns:
        None

    Raises:
        wandb.errors.CommError: If there is a communication error while sending the alert.
        wandb.errors.UsageError: If there is a usage error while sending the alert.
        Exception: If there is an unexpected error while sending the alert.
    """
    if wandb_notifications and wandb.run:
        try:
            # Replace the user's home directory with '[USER_HOME]' in the alert text
            text = str(text).replace(str(models_path), "[REDACTED]")
            wandb.alert(
                title=title,
                text=text,
                level=level,
            )
        except wandb.errors.CommError as e:
            logger.error(f"Communication error sending WandB alert: {e}")
        except wandb.errors.UsageError as e:
            logger.error(f"Usage error sending WandB alert: {e}")
        except Exception as e:
            logger.error(f"Unexpected error sending WandB alert: {e}")