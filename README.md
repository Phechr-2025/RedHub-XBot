# RedHub-XBot

บอท Discord สำหรับขาย VPN/โครงข่าย ที่ยังใช้ SQLite และ 3x-ui API เดิม

## ติดตั้งบน VPS แบบคำสั่งเดียว

รันคำสั่งนี้ใน VPS ได้เลย:

```bash
git clone https://github.com/Phechr-2025/RedHub-XBot.git && cd RedHub-XBot && chmod +x install_vps.sh && sudo ./install_vps.sh
```

สคริปต์จะ:
- ติดตั้ง Python และ dependencies ที่จำเป็น
- สร้าง virtual environment
- ถามก่อนว่าต้องการตั้งค่า `.env` เลยหรือเก็บไว้ตั้งทีหลัง
- สร้าง service ชื่อเดียวจาก `APP_SLUG`
- ติดตั้งคำสั่งเมนูจากตัวแปร `MENU_COMMAND` ให้เรียกได้จากทุกที่
- บันทึกโฟลเดอร์โปรเจกต์ไว้ที่ `/home/ubuntu/<APP_SLUG>`
- ดาวน์โหลดไฟล์จาก GitHub Release ล่าสุดตอนติดตั้ง และตอนอัปเดตจะตรวจเวอร์ชั่นก่อน/หลังให้อัตโนมัติ
- หากต้องการเปลี่ยนชื่อโปรเจกต์ในอนาคต แก้ `PROJECT_NAME` ที่บรรทัดบนสุดของ `install_vps.sh` เพียงจุดเดียว
- หากต้องการเปลี่ยนคำสั่งเรียกเมนู แก้ `MENU_COMMAND` ที่บรรทัดบนสุดของ `install_vps.sh` เพียงจุดเดียว
- สร้างไฟล์ `/etc/menubot.conf` สำหรับให้คำสั่งเมนูรู้ path ของโปรเจกต์
- เมื่อจบการติดตั้งจะนับถอยหลัง 10 วินาทีแล้วรีบูตเครื่อง

ถ้าเลือกเก็บ `.env` ไว้ภายหลัง ให้เข้า command เมนูที่กำหนดใน `MENU_COMMAND` แล้วเลือกเมนู `4) จัดการเว็บไซต์` เพื่อแก้ค่าผ่านเว็บแทน

## ไฟล์ที่ต้องเตรียม

ก่อนรัน ให้เตรียมข้อมูลเหล่านี้ไว้:
- Discord bot token
- Discord user ID ของแอดมิน
- 3x-ui URL
- 3x-ui API token หรือ username/password สำหรับ login แบบ session
- AIS inbound ID
- TRUE inbound ID
- เบอร์ wallet สำหรับรับเงิน
- ตำแหน่งฐานข้อมูล SQLite (ค่ามาตรฐานคือ `/data/bot.db`)

## คำสั่งรันหลัก

- `!start`
- `!mycredit`
- `!checkprice`
- `!addclient`
- `!freeclient`
- `!mycodes`
- `!addmycredit`
- `!entercode`

## คำสั่งแอดมิน

- `!addcredits @user 10`
- `!deletecredits @user 10`
- `!setprice 2`
- `!settingsmycredit`
- `!setangpaophone 0xxxxxxxxx`
- `!setangpaorate 1.5`
- `!checkangpaophone`
- `!toggleaddclient`
- `!buydm`
- `!nobuydm`
- `!openfreeclient`
- `!offfreeclient`
- `!freeclientlimit 1`
- `!freeclienttime 1`
- `!freeclientresettime midnight`
- `!resetfreeclientlimit @user`
- `!addcode`
- `!deletecode ชื่อโค้ด`
- `!checkcode`
- `!statuscode on`
- `!checkusercode ชื่อโค้ด`
- `!logbuy @user`
- `!logfree @user`
- `!logbuyall`
- `!logfreeall`

## เมนูควบคุมบน VPS

เมื่อบอทรันอยู่บน VPS ให้พิมพ์คำสั่งเมนูที่ตั้งไว้ใน `MENU_COMMAND` ในแชทของแอดมินเพื่อเปิดเมนูควบคุม:

1. ถอนการติดตั้ง — ยืนยัน 2 รอบก่อนดำเนินการ และลบไฟล์ทั้งหมดที่ติดตั้ง/ดาวน์โหลดมา จากนั้นนับถอยหลัง 10 วินาทีแล้วรีบูตเครื่อง
2. ดูสถานะการทำงานบอท — แสดงสถานะของ service บอทเท่านั้น พร้อมเวอร์ชันจากชื่อ tag
3. รีสตาร์ทระบบบอท — ยืนยัน 1 รอบ ข้อมูลในฐานข้อมูลไม่หาย
4. จัดการเว็บไซต์ — เข้าเมนูเว็บพาเนลสำหรับดู URL, เปลี่ยน path/port, เพิ่มโดเมน, อัปเดตไลบารี่เว็บ และเช็คสถานะเว็บ
5. อัปเดตสคริประบบ — แสดงเวอร์ชันก่อนอัปเดตและเวอร์ชันล่าสุดที่ตรวจพบ จากนั้นดึง Release ล่าสุดมาใช้
6. อัปเดตไลบารี่บอท — อัปเดต dependencies ใน virtualenv ของบอท
7. อัปเดตไลบารี่ที่สคริปต์ — ติดตั้ง/อัปเดตแพ็กเกจที่สคริปต์ควรใช้ในระบบ
0. ออก — ปิดเมนู

คำสั่งเมนูใช้ไฟล์ตั้งค่าที่ `/etc/menubot.conf` จึงเรียกใช้ได้จากทุกที่โดยไม่ต้องอยู่ในโฟลเดอร์โปรเจกต์
เมนูถอนการติดตั้งจะลบไฟล์ที่ติดตั้ง/ดาวน์โหลดทั้งหมด แล้วนับถอยหลัง 10 วินาทีก่อนสั่ง reboot เครื่อง

## เว็บพาเนลตั้งค่า

ระบบจะติดตั้งเว็บพาเนลแยกจากบอทเป็น service อีกตัวหนึ่ง

- เปิดด้วย URL ตาม `WEB_HTTP_PORT` และ path ที่สุ่มไว้ใน `WEB_PANEL_PATH` (ระบบจะทำ reverse proxy ไปที่ `WEB_PORT` ให้อัตโนมัติ)
- ถ้าเปิดแค่ `http://IP:WEB_HTTP_PORT` ระบบจะพาไปยัง path ที่สุ่มไว้ให้อัตโนมัติ
- แก้ค่า `.env` ผ่านหน้าเว็บได้เลย

- หน้าตา Web Panel ทำเป็น sidebar คล้าย 3x-ui รุ่นใหม่ มีปุ่มสามขีดซ้ายบน และเมนู 2 รายการคือ `ภาพรวม` กับ `ตั้งค่าทั่วไป`
- เมนูจัดการเว็บไซต์มีคำสั่งแยกชัดเจนสำหรับดู URL, สถานะ, รีสตาร์ท, เปลี่ยน path/port, เพิ่มโดเมน และอัปเดตไลบารี่เว็บ

- ถ้าผูกโดเมนและ DNS ชี้มาที่ VPS แล้ว ระบบจะติดตั้ง nginx + Let's Encrypt ให้โดยอัตโนมัติ
- เพิ่มโดเมนภายหลังได้จากเมนู `4) จัดการเว็บไซต์`
- หากต้องการสุ่ม path ใหม่ ใช้เมนูจัดการเว็บไซต์หรือคำสั่ง `webctl reset`


## รันแบบ manual

ถ้าไม่อยากใช้ service ให้รันเองได้ด้วย:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
python main.py
```

## หมายเหตุ

- คำสั่งย่อยส่วนใหญ่ใช้ข้อความโต้ตอบแบบทีละขั้นในห้องแชท
- ลิงก์/โค้ด VPN และเครดิตยังใช้ฐานข้อมูลเดิม
- ถ้าต้องการแปลงเป็น slash commands แบบแท้ ๆ ของ Discord เพิ่มได้ภายหลัง
