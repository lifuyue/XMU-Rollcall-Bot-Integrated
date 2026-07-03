import time
from .notifier import build_notifier
from .qr_handler import handle_qr_rollcall
from .verify import send_code, send_radar

def process_rollcalls(data, session, account=None):
    """处理签到数据"""
    data_empty = {'rollcalls': []}
    result = handle_rollcalls(data, session, account=account)
    if False in result:
        return data_empty
    else:
        return data

def extract_rollcalls(data, account=None):
    """提取签到信息"""
    rollcalls = data['rollcalls']
    result = []
    account_info = {}
    if account:
        account_info = {
            "account_id": account.get("id"),
            "account_name": account.get("name") or account.get("username"),
        }
    if rollcalls:
        rollcall_count = len(rollcalls)
        for rollcall in rollcalls:
            item = {
                'course_title': rollcall['course_title'],
                'created_by_name': rollcall['created_by_name'],
                'department_name': rollcall['department_name'],
                'is_expired': rollcall['is_expired'],
                'is_number': rollcall['is_number'],
                'is_radar': rollcall['is_radar'],
                'rollcall_id': rollcall['rollcall_id'],
                'rollcall_status': rollcall['rollcall_status'],
                'scored': rollcall['scored'],
                'status': rollcall['status']
            }
            item.update(account_info)
            result.append(item)
    else:
        rollcall_count = 0
    return rollcall_count, result

def handle_rollcalls(data, session, account=None):
    """处理签到流程"""
    count, rollcalls = extract_rollcalls(data, account=account)
    answer_status = [False for _ in range(count)]
    notifier = build_notifier()

    if count:
        print(time.strftime("%H:%M:%S", time.localtime()), f"New rollcall(s) found!\n")
        for i in range(count):
            print(f"{i+1} of {count}:")
            print(f"Course name: {rollcalls[i]['course_title']}, rollcall created by {rollcalls[i]['department_name']} {rollcalls[i]['created_by_name']}.")

            if rollcalls[i]['is_radar']:
                temp_str = "Radar rollcall"
            elif rollcalls[i]['is_number']:
                temp_str = "Number rollcall"
            else:
                temp_str = "QRcode rollcall"
            print(f"Rollcall type: {temp_str}\n")

            if rollcalls[i]['status'] == 'on_call_fine':
                print("Already answered.")
                answer_status[i] = True
            elif rollcalls[i]['is_expired']:
                print("Rollcall is expired.")
                answer_status[i] = True
            elif rollcalls[i]['status'] != 'absent':
                print(f"Rollcall status is {rollcalls[i]['status']}; no answer attempt needed.")
                answer_status[i] = True
            elif rollcalls[i]['is_radar']:
                if send_radar(session, rollcalls[i]['rollcall_id']):
                    answer_status[i] = True
                    _notify_success(notifier, rollcalls[i], "Radar")
                else:
                    print("Answering failed.")
            elif rollcalls[i]['is_number']:
                if send_code(session, rollcalls[i]['rollcall_id']):
                    answer_status[i] = True
                    _notify_success(notifier, rollcalls[i], "Number")
                else:
                    print("Answering failed.")
            else:
                if handle_qr_rollcall(session, rollcalls[i]):
                    answer_status[i] = True
                    _notify_success(notifier, rollcalls[i], "QRcode")
                else:
                    print("Answering failed. QRcode rollcall was not completed.")

    return answer_status

def _notify_success(notifier, rollcall, rollcall_type):
    result = notifier.send_rollcall_success(rollcall, rollcall_type)
    if not result.ok:
        print(f"[Notification] Success feedback failed: {result.message}")
