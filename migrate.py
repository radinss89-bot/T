import os
import psycopg2

OLD_DATA = {
    "6235380364": {"coins": 350, "last": 1788296325.4894392, "name": "انگیریبرت"},
    "136817688": {"coins": 90, "last": 1788294902.1632848, "name": "Channel"},
    "8865416131": {"coins": 80, "last": 1788290772.6254506, "name": "𝒵𝒾𝓏𝒾"},
    "6031330799": {"coins": 130, "last": 1788296764.9112263, "name": "ALI"},
    "6713806945": {"coins": 540, "last": 1788297289.6357033, "name": "Artin"},
    "7561857278": {"coins": 0, "last": 1788296500.4368348, "name": "Saman"},
    "7514812387": {"coins": 20, "last": 1788290786.1936092, "name": "Nima"},
    "7220693915": {"coins": 30, "last": 1788276236.5996058, "name": "𝕊𝕚𝕟𝕚𝕤𝕥𝕖𝕣 𝕞𝕒𝕣𝕜"},
    "8999839272": {"coins": 5135, "last": 1788297280.7522817, "name": "29"},
    "8587357724": {"coins": 30, "last": 1788274826.742062, "name": "Gholamali"},
    "7849336808": {"coins": 50, "last": 1788297000.939751, "name": "Aseman"},
    "6682453094": {"coins": 70, "last": 1788298181.0538397, "name": "𝓐𝓶𝓲𝓻𝓱𝓸𝓼𝓼𝓮𝓲𝓷"},
    "8821117176": {"coins": 40, "last": 1788290826.9027965, "name": "Parsa"},
    "1087968824": {"coins": 10, "last": 1788261545.353072, "name": "Group"},
    "8582975779": {"coins": 500, "last": 1788293765.360082, "name": "CCCAMR7"},
    "8451365696": {"coins": 20, "last": 1788296762.8972645, "name": "𝔇𝔢𝔫𝔧𝔦"},
    "8567100992": {"coins": 30, "last": 1788276224.5053937, "name": "ali⚛️"},
    "7235327599": {"coins": 90, "last": 1788275771.5670612, "name": "Alireza"},
    "8659085236": {"coins": 150, "last": 1788294721.399373, "name": "Mehrad"},
    "8452372451": {"coins": 20, "last": 0, "name": "LEON."},
    "8998649485": {"coins": 10, "last": 1788276864.5904436, "name": "-DeMoN"},
    "5182651513": {"coins": 140, "last": 1788296147.7769482, "name": "XC"},
    "8780396158": {"coins": 10, "last": 1788277873.6767304, "name": "امیر حسین"},
    "7249531552": {"coins": 10, "last": 1788277896.5597332, "name": "Truck-sama"},
    "8888227469": {"coins": 20, "last": 1788298172.7413194, "name": "𝕲𝘳𝘢𝘤𝘦 ᥲsһᥴr᥆𝖿𝗍 ☥"},
    "8695500069": {"coins": 20, "last": 1788278291.012472, "name": "𝑺𝒊𝒆𝒕𝑳𝒆𝒔𝒔"},
    "8899202914": {"coins": 10, "last": 1788278240.6047862, "name": "𝔼HSAℕ"},
    "7457077807": {"coins": 10, "last": 1788281261.1140058, "name": "𝙕𝙀𝙉"},
    "8972862398": {"coins": 660, "last": 1788297273.8118484, "name": "𝗤"},
    "7956848487": {"coins": 140, "last": 1788295118.9153965, "name": "جان پارک 3:"},
    "8594435724": {"coins": 0, "last": 0, "name": "ربات فولکی"},
    "8482970210": {"coins": 10, "last": 1788292777.219719, "name": "Makan"},
    "6510533481": {"coins": 10, "last": 1788295168.9278586, "name": "Нуар (literal loser)"},
    "7920291123": {"coins": 110, "last": 1788295708.2017288, "name": "ⓢⓞⓡⓔⓝⓐ~¹⁰⁰¹"},
    "635352090": {"coins": 0, "last": 0, "name": "↻ DIGI ANTI ⇦"},
    "6135118569": {"coins": 120, "last": 1788296324.2724352, "name": "Ali"},
    "6133729617": {"coins": 10, "last": 1788297027.5955634, "name": "El Nino #14"}
}


conn = psycopg2.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()

for user_id, user in OLD_DATA.items():
    cur.execute("""
        INSERT INTO users (user_id, name, coins, last_message)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (user_id)
        DO UPDATE SET
            name = EXCLUDED.name,
            coins = EXCLUDED.coins,
            last_message = EXCLUDED.last_message
    """, (
        int(user_id),
        user["name"],
        user["coins"],
        user["last"]
    ))

conn.commit()

print(f"✅ {len(OLD_DATA)} کاربر منتقل شد!")

cur.close()
conn.close()