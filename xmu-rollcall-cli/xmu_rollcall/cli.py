import click
import sys
from xmulogin import xmulogin
from . import __version__
from .config import (
    load_config, save_config, is_config_complete, get_cookies_path,
    add_account, get_all_accounts, get_current_account, set_current_account,
    get_account_by_id, CONFIG_FILE, delete_account, perform_account_deletion,
    get_notification_config,
)
from .monitor import start_monitor, base_url, headers
from .multi_monitor import is_account_complete, start_multi_account_monitor

# ANSI Color codes
class Colors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    GRAY = '\033[90m'

@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx):
    if ctx.invoked_subcommand is None:
        click.echo(f"{Colors.OKCYAN}{Colors.BOLD}XMU Rollcall Bot CLI v{__version__}{Colors.ENDC}")
        click.echo(f"\nUsage:")
        click.echo(f"  xmu config    Configure credentials and add accounts")
        click.echo(f"  xmu add-account Add one account quickly")
        click.echo(f"  xmu switch    Switch between accounts")
        click.echo(f"  xmu start     Start monitoring rollcalls")
        click.echo(f"  xmu start-all Start monitoring all configured accounts")
        click.echo(f"  xmu wechat-login Login WeChat notifier by QR scan")
        click.echo(f"  xmu wechat-bind Bind a target WeChat chat for notifications")
        click.echo(f"  xmu wechat-test Send a WeChat notification test")
        click.echo(f"  xmu refresh   Refresh the login status")
        click.echo(f"  xmu --help    Show this message")

def add_account_interactive(current_config):
    """添加新账号"""
    click.echo(f"{Colors.BOLD}Adding a new account...{Colors.ENDC}\n")

    username = click.prompt(f"{Colors.BOLD}Username{Colors.ENDC}")
    password = click.prompt(f"{Colors.BOLD}Password{Colors.ENDC}", hide_input=True)

    click.echo(f"\n{Colors.OKCYAN}Validating credentials...{Colors.ENDC}")
    try:
        session = xmulogin(type=3, username=username, password=password)
        if session:
            click.echo(f"{Colors.OKGREEN}✓ Login successful!{Colors.ENDC}")

            click.echo(f"{Colors.OKCYAN}Fetching user profile...{Colors.ENDC}")
            try:
                profile = session.get(f"{base_url}/api/profile", headers=headers).json()
                name = profile.get("name", "")
                click.echo(f"{Colors.OKGREEN}✓ Welcome, {name}!{Colors.ENDC}")
            except Exception:
                click.echo(f"{Colors.WARNING}⚠ Could not fetch profile, using username as name{Colors.ENDC}")
                name = username

            try:
                account_id = add_account(current_config, username, password, name)
                save_config(current_config)

                click.echo(f"{Colors.OKGREEN}✓ Account added successfully! (ID: {account_id}){Colors.ENDC}")
                click.echo(f"{Colors.GRAY}Configuration file: {CONFIG_FILE}{Colors.ENDC}\n")
                return True
            except RuntimeError as e:
                click.echo(f"{Colors.FAIL}✗ Failed to save configuration: {str(e)}{Colors.ENDC}")
                click.echo(f"{Colors.WARNING}Tip: In sandboxed environments (like a-Shell), set environment variable:{Colors.ENDC}")
                click.echo(f"  export XMU_ROLLCALL_CONFIG_DIR=~/Documents/.xmu_rollcall")
        else:
            click.echo(f"{Colors.FAIL}✗ Login failed. Please check your credentials.{Colors.ENDC}")
    except Exception as e:
        click.echo(f"{Colors.FAIL}✗ Error during login validation: {str(e)}{Colors.ENDC}")
    return False

@cli.command()
def config():
    """配置账号：添加、删除账号"""
    click.echo(f"\n{Colors.BOLD}{Colors.OKCYAN}=== XMU Rollcall Configuration ==={Colors.ENDC}\n")

    current_config = load_config()

    def show_accounts():
        """显示账号列表"""
        accounts = get_all_accounts(current_config)
        if accounts:
            click.echo(f"{Colors.BOLD}Existing accounts:{Colors.ENDC}")
            current_account = get_current_account(current_config)
            for acc in accounts:
                current_marker = f" {Colors.OKGREEN}(current){Colors.ENDC}" if current_account and acc.get("id") == current_account.get("id") else ""
                click.echo(f"  {acc.get('id')}: {acc.get('name') or acc.get('username')}{current_marker}")
            click.echo()
        else:
            click.echo(f"{Colors.GRAY}No accounts configured.{Colors.ENDC}\n")

    def delete_existing_account():
        """删除账号"""
        accounts = get_all_accounts(current_config)
        if not accounts:
            click.echo(f"{Colors.WARNING}No accounts to delete.{Colors.ENDC}\n")
            return

        show_accounts()

        # 让用户选择要删除的账号
        valid_ids = [str(acc.get("id")) for acc in accounts]
        selected_id = click.prompt(
            f"{Colors.BOLD}Enter account ID to delete{Colors.ENDC}",
            type=click.Choice(valid_ids, case_sensitive=False)
        )

        selected_id = int(selected_id)
        selected_account = get_account_by_id(current_config, selected_id)

        if selected_account:
            # 确认删除
            confirm = click.prompt(
                f"{Colors.WARNING}Are you sure you want to delete account '{selected_account.get('name') or selected_account.get('username')}' (ID: {selected_id})?{Colors.ENDC}",
                type=click.Choice(['y', 'n'], case_sensitive=False),
                default='n'
            )

            if confirm.lower() == 'y':
                # 执行删除
                success, cookies_to_delete, cookies_to_rename = delete_account(current_config, selected_id)

                if success:
                    # 保存配置
                    save_config(current_config)

                    # 处理cookies文件
                    perform_account_deletion(cookies_to_delete, cookies_to_rename)

                    click.echo(f"{Colors.OKGREEN}✓ Account deleted successfully!{Colors.ENDC}")

                    # 显示ID变更提示
                    if cookies_to_rename:
                        click.echo(f"{Colors.GRAY}Note: Account IDs have been re-assigned.{Colors.ENDC}")
                    click.echo()
                else:
                    click.echo(f"{Colors.FAIL}✗ Failed to delete account.{Colors.ENDC}\n")
            else:
                click.echo(f"{Colors.GRAY}Deletion cancelled.{Colors.ENDC}\n")
        else:
            click.echo(f"{Colors.FAIL}✗ Account not found.{Colors.ENDC}\n")

    # 主循环
    while True:
        show_accounts()

        click.echo(f"{Colors.BOLD}Choose an action:{Colors.ENDC}")
        click.echo(f"  {Colors.OKCYAN}n{Colors.ENDC} - Add new account")
        click.echo(f"  {Colors.OKCYAN}d{Colors.ENDC} - Delete account")
        click.echo(f"  {Colors.OKCYAN}q{Colors.ENDC} - Quit")

        action = click.prompt(
            f"\n{Colors.BOLD}Action{Colors.ENDC}",
            type=click.Choice(['n', 'd', 'q'], case_sensitive=False),
            default='q'
        )

        click.echo()

        if action.lower() == 'n':
            add_account_interactive(current_config)
        elif action.lower() == 'd':
            delete_existing_account()
        elif action.lower() == 'q':
            # 退出前显示最终账号列表
            accounts = get_all_accounts(current_config)
            if accounts:
                click.echo(f"{Colors.BOLD}Final account list:{Colors.ENDC}")
                current_account = get_current_account(current_config)
                for acc in accounts:
                    current_marker = f" {Colors.OKGREEN}(current){Colors.ENDC}" if current_account and acc.get("id") == current_account.get("id") else ""
                    click.echo(f"  {acc.get('id')}: {acc.get('name') or acc.get('username')}{current_marker}")
                click.echo(f"\n{Colors.GRAY}You can run: {Colors.BOLD}xmu switch{Colors.ENDC} to switch between accounts")
                click.echo(f"{Colors.GRAY}You can run: {Colors.BOLD}xmu start{Colors.ENDC} to start monitoring")
            break

@cli.command("add-account")
def add_account_command():
    """快速添加一个账号"""
    current_config = load_config()
    add_account_interactive(current_config)

@cli.command()
def start():
    """启动签到监控"""
    # 加载配置
    config_data = load_config()

    # 检查配置是否完整
    if not is_config_complete(config_data):
        click.echo(f"{Colors.FAIL}✗ Configuration incomplete!{Colors.ENDC}")
        click.echo(f"Please run: {Colors.BOLD}xmu config{Colors.ENDC}")
        sys.exit(1)

    # 获取当前账号
    current_account = get_current_account(config_data)
    click.echo(f"{Colors.OKCYAN}Using account: {current_account.get('name') or current_account.get('username')} (ID: {current_account.get('id')}){Colors.ENDC}")

    # 启动监控
    try:
        start_monitor(current_account)
    except KeyboardInterrupt:
        click.echo(f"\n{Colors.WARNING}Shutting down...{Colors.ENDC}")
        sys.exit(0)
    except Exception as e:
        click.echo(f"\n{Colors.FAIL}Error: {str(e)}{Colors.ENDC}")
        sys.exit(1)

@cli.command("start-all")
@click.option("--interval", "poll_interval", default=1.0, show_default=True, type=float, help="Polling interval per account in seconds.")
@click.option("--account-id", "account_ids", multiple=True, type=int, help="Monitor only selected account ID(s). Repeat for multiple accounts.")
def start_all(poll_interval, account_ids):
    """启动所有已配置账号的签到监控"""
    config_data = load_config()
    accounts = [account for account in get_all_accounts(config_data) if is_account_complete(account)]

    if account_ids:
        selected = set(account_ids)
        accounts = [account for account in accounts if account.get("id") in selected]

    if not accounts:
        click.echo(f"{Colors.FAIL}✗ No complete account configured!{Colors.ENDC}")
        click.echo(f"Please run: {Colors.BOLD}xmu add-account{Colors.ENDC}")
        sys.exit(1)

    click.echo(f"{Colors.OKCYAN}Monitoring {len(accounts)} account(s):{Colors.ENDC}")
    for account in accounts:
        click.echo(f"  {account.get('id')}: {account.get('name') or account.get('username')}")

    try:
        start_multi_account_monitor(accounts, poll_interval=poll_interval)
    except KeyboardInterrupt:
        click.echo(f"\n{Colors.WARNING}Shutting down...{Colors.ENDC}")
        sys.exit(0)
    except Exception as e:
        click.echo(f"\n{Colors.FAIL}Error: {str(e)}{Colors.ENDC}")
        sys.exit(1)

@cli.command()
def refresh():
    """清除当前账号的登录缓存"""
    config_data = load_config()
    current_account = get_current_account(config_data)

    if not current_account:
        click.echo(f"{Colors.FAIL}✗ No account configured!{Colors.ENDC}")
        click.echo(f"Please run: {Colors.BOLD}xmu config{Colors.ENDC}")
        sys.exit(1)

    account_id = current_account.get("id")
    cookies_path = get_cookies_path(account_id)
    try:
        click.echo(f"\n{Colors.WARNING}Deleting cookies for account {account_id} ({current_account.get('name')})...{Colors.ENDC}")
        # delete cookies file
        import os
        if os.path.exists(cookies_path):
            os.remove(cookies_path)
            click.echo(f"{Colors.OKGREEN}✓ Cookies deleted successfully.{Colors.ENDC}")
        else:
            click.echo(f"{Colors.GRAY}No cookies file found to delete.{Colors.ENDC}")
        sys.exit(0)
    except Exception as e:
        click.echo(f"{Colors.FAIL}✗ Failed to delete cookies: {str(e)}{Colors.ENDC}")
        sys.exit(1)


@cli.command()
def switch():
    """切换当前使用的账号"""
    click.echo(f"\n{Colors.BOLD}{Colors.OKCYAN}=== Switch Account ==={Colors.ENDC}\n")

    config_data = load_config()
    accounts = get_all_accounts(config_data)

    if not accounts:
        click.echo(f"{Colors.FAIL}✗ No accounts configured!{Colors.ENDC}")
        click.echo(f"Please run: {Colors.BOLD}xmu config{Colors.ENDC}")
        sys.exit(1)

    current_account = get_current_account(config_data)
    current_id = current_account.get("id") if current_account else None

    # 显示账号列表
    click.echo(f"{Colors.BOLD}Available accounts:{Colors.ENDC}")
    for acc in accounts:
        current_marker = f" {Colors.OKGREEN}(current){Colors.ENDC}" if acc.get("id") == current_id else ""
        click.echo(f"  {acc.get('id')}: {acc.get('name') or acc.get('username')}{current_marker}")

    click.echo()

    # 让用户选择账号
    valid_ids = [str(acc.get("id")) for acc in accounts]
    selected_id = click.prompt(
        f"{Colors.BOLD}Enter account ID to switch to{Colors.ENDC}",
        type=click.Choice(valid_ids, case_sensitive=False)
    )

    selected_id = int(selected_id)
    selected_account = get_account_by_id(config_data, selected_id)

    if selected_account:
        set_current_account(config_data, selected_id)
        save_config(config_data)
        click.echo(f"\n{Colors.OKGREEN}✓ Switched to account: {selected_account.get('name') or selected_account.get('username')} (ID: {selected_id}){Colors.ENDC}")
        click.echo(f"{Colors.GRAY}You can now run: {Colors.BOLD}xmu start{Colors.ENDC}")
    else:
        click.echo(f"{Colors.FAIL}✗ Account not found!{Colors.ENDC}")
        sys.exit(1)


def _get_wechat_config():
    config_data = load_config()
    return get_notification_config(config_data).get("wechat") or {}


@cli.command("wechat-login")
@click.option("--force", is_flag=True, help="Ignore existing WeChat credentials and scan again.")
def wechat_login(force):
    """扫码登录微信通知机器人并保存凭证"""
    from .wechat_notifier import run_wechat_login

    try:
        run_wechat_login(_get_wechat_config(), force=force)
        click.echo(f"{Colors.OKGREEN}✓ WeChat login completed.{Colors.ENDC}")
    except KeyboardInterrupt:
        click.echo(f"\n{Colors.WARNING}WeChat login cancelled.{Colors.ENDC}")
        sys.exit(1)
    except Exception as e:
        click.echo(f"{Colors.FAIL}✗ WeChat login failed: {str(e)}{Colors.ENDC}")
        sys.exit(1)


@cli.command("wechat-bind")
def wechat_bind():
    """等待目标微信群任意消息并保存主动发送上下文"""
    from .wechat_notifier import run_wechat_bind

    click.echo("After this command starts, send any message in the target WeChat chat.")
    click.echo("The bot will only save the chat context and will not reply.")
    try:
        run_wechat_bind(_get_wechat_config())
        click.echo(f"{Colors.OKGREEN}✓ WeChat target chat bound.{Colors.ENDC}")
    except KeyboardInterrupt:
        click.echo(f"\n{Colors.WARNING}WeChat bind cancelled.{Colors.ENDC}")
        sys.exit(1)
    except Exception as e:
        click.echo(f"{Colors.FAIL}✗ WeChat bind failed: {str(e)}{Colors.ENDC}")
        sys.exit(1)


@cli.command("wechat-test")
@click.option("--text", default=None, help="Custom test message text.")
def wechat_test(text):
    """向已绑定微信群发送测试消息"""
    from .wechat_notifier import run_wechat_test

    result = run_wechat_test(_get_wechat_config(), text=text)
    if result.ok:
        click.echo(f"{Colors.OKGREEN}✓ WeChat test completed: {result.message}{Colors.ENDC}")
    else:
        click.echo(f"{Colors.FAIL}✗ WeChat test failed: {result.message}{Colors.ENDC}")
        sys.exit(1)


if __name__ == '__main__':
    cli()
