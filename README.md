# Discord VPN Shop Bot

บอท Discord ที่พอร์ตจาก Telegram bot เดิม โดยยังใช้ SQLite และ 3x-ui API เดิม

## ติดตั้งบน VPS แบบคำสั่งเดียว

รันคำสั่งนี้ใน VPS ได้เลย:

```bash
git clone https://github.com/Phechr2554/Discord-shop3x.git && cd Discord-shop3x && chmod +x install_vps.sh && sudo ./install_vps.sh
```

สคริปต์จะ:
- ติดตั้ง Python และ dependencies ที่จำเป็น
- สร้าง virtual environment
- ถามก่อนว่าต้องการตั้งค่า `.env` เลยหรือเก็บไว้ตั้งทีหลัง
- สร้าง service ชื่อ `xbot`
- ติดตั้งคำสั่ง `menubot` ให้เรียกได้จากทุกที่
- บันทึกโฟลเดอร์โปรเจกต์ไว้ที่ `/home/ubuntu/xbot`
- สร้างไฟล์ `/etc/menubot.conf` สำหรับให้ `menubot` รู้ path ของโปรเจกต์
- เมื่อจบการติดตั้งจะนับถอยหลัง 10 วินาทีแล้วรีบูตเครื่อง

ถ้าเลือกเก็บ `.env` ไว้ภายหลัง ให้เข้า `menubot` แล้วเลือกเมนู `6) ตั้งค่า .env`

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

เมื่อบอทรันอยู่บน VPS ให้พิมพ์ `menubot` ในแชทของแอดมินเพื่อเปิดเมนูควบคุม:

1. ถอนการติดตั้ง — ยืนยัน 2 รอบก่อนดำเนินการ และลบไฟล์ทั้งหมดที่ติดตั้ง/ดาวน์โหลดมา จากนั้นนับถอยหลัง 10 วินาทีแล้วรีบูตเครื่อง
2. ดูสถานะการทำงาน
3. รีสตาร์ทระบบ — ยืนยัน 1 รอบ ข้อมูลในฐานข้อมูลไม่หาย
5. อัปเดตสคริประบบ — ยืนยัน 1 รอบ
6. ตั้งค่า `.env` — เปิดใน `nano` เพื่อแก้ค่าแล้วกด `Ctrl+O` / `Ctrl+X` เพื่อบันทึก

`menubot` ใช้ไฟล์ตั้งค่าที่ `/etc/menubot.conf` จึงเรียกใช้ได้จากทุกที่โดยไม่ต้องอยู่ในโฟลเดอร์โปรเจกต์
เมนูถอนการติดตั้งจะลบไฟล์ที่ติดตั้ง/ดาวน์โหลดทั้งหมด แล้วนับถอยหลัง 10 วินาทีก่อนสั่ง reboot เครื่อง

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
