// =====================================
// CONFIG
// =====================================

// آدرس API خودت را اینجا قرار بده

const API_URL = "https://YOUR-API.onrender.com";


// =====================================
// TELEGRAM
// =====================================

const tg = window.Telegram?.WebApp;

if (tg) {
    tg.ready();
    tg.expand();
}


// =====================================
// USER
// =====================================

let userId = null;

if (tg && tg.initDataUnsafe?.user) {

    userId =
        tg.initDataUnsafe.user.id;

}


// برای تست مرورگر عادی
if (!userId) {

    const params =
        new URLSearchParams(
            window.location.search
        );

    userId =
        params.get("user_id");
}


// =====================================
// DATA
// =====================================

let currentPrice = 0;

let previousPrice = 0;

let history = [];


// =====================================
// ELEMENTS
// =====================================

const coinsEl =
    document.getElementById("coins");

const priceEl =
    document.getElementById("price");

const stockNameEl =
    document.getElementById("stockName");

const changeEl =
    document.getElementById("change");

const amountEl =
    document.getElementById("amount");

const totalEl =
    document.getElementById("total");

const sharesEl =
    document.getElementById("shares");

const assetValueEl =
    document.getElementById("assetValue");

const statusEl =
    document.getElementById("status");

const canvas =
    document.getElementById("chart");

const ctx =
    canvas.getContext("2d");


// =====================================
// API HELPER
// =====================================

async function api(
    endpoint,
    options = {}
) {

    const response =
        await fetch(
            API_URL + endpoint,
            {
                ...options,

                headers: {
                    "Content-Type":
                        "application/json",

                    ...(options.headers || {})
                }
            }
        );

    const data =
        await response.json();

    if (!response.ok) {

        throw new Error(
            data.error ||
            "خطای سرور"
        );
    }

    return data;
}


// =====================================
// LOAD MARKET
// =====================================

async function loadMarket() {

    try {

        const data =
            await api("/market");

        const stock =
            data.stock;

        stockNameEl.textContent =
            stock.name;

        previousPrice =
            currentPrice;

        currentPrice =
            stock.price;

        priceEl.textContent =
            currentPrice.toLocaleString();

        if (
            previousPrice &&
            previousPrice !== currentPrice
        ) {

            const percent =
                (
                    (currentPrice -
                        previousPrice)
                    /
                    previousPrice
                ) * 100;

            changeEl.textContent =
                (
                    percent >= 0
                    ? "↗ +"
                    : "↘ "
                )
                +
                percent.toFixed(2)
                +
                "%";
        }

        history.push(currentPrice);

        if (history.length > 40) {
            history.shift();
        }

        drawChart();

        updateTotal();

        statusEl.textContent =
            "● آنلاین";

    }

    catch (error) {

        console.error(error);

        statusEl.textContent =
            "● خطا در اتصال";

        showToast(
            "اتصال به API برقرار نشد"
        );
    }
}


// =====================================
// LOAD WALLET
// =====================================

async function loadWallet() {

    if (!userId) {

        coinsEl.textContent =
            "نامشخص";

        return;
    }

    try {

        const data =
            await api(
                "/wallet/" + userId
            );

        coinsEl.textContent =
            Number(
                data.coins
            ).toLocaleString();

    }

    catch (error) {

        console.error(error);
    }
}


// =====================================
// AMOUNT
// =====================================

function changeAmount(value) {

    let amount =
        Number(
            amountEl.value
        ) || 1;

    amount += value;

    if (amount < 1) {
        amount = 1;
    }

    amountEl.value =
        amount;

    updateTotal();
}


amountEl.addEventListener(
    "input",
    updateTotal
);


// =====================================
// TOTAL
// =====================================

function updateTotal() {

    const amount =
        Number(
            amountEl.value
        ) || 0;

    const total =
        amount *
        currentPrice;

    totalEl.textContent =
        total.toLocaleString();

    assetValueEl.textContent =
        (
            Number(
                sharesEl.textContent
            ) *
            currentPrice
        ).toLocaleString();
}


// =====================================
// BUY
// =====================================

async function buyStock() {

    if (!userId) {

        showToast(
            "شناسه کاربر پیدا نشد"
        );

        return;
    }

    const amount =
        Number(
            amountEl.value
        );

    if (
        !Number.isInteger(amount) ||
        amount <= 0
    ) {

        showToast(
            "تعداد نامعتبر است"
        );

        return;
    }

    try {

        const data =
            await api(
                "/buy",
                {
                    method: "POST",

                    body: JSON.stringify({
                        user_id: userId,
                        amount: amount
                    })
                }
            );

        showToast(
            `✅ خرید انجام شد — ${data.total.toLocaleString()} سکه`
        );

        await loadWallet();

        await loadPortfolio();

    }

    catch (error) {

        showToast(
            "❌ " + error.message
        );
    }
}


// =====================================
// SELL
// =====================================

async function sellStock() {

    if (!userId) {

        showToast(
            "شناسه کاربر پیدا نشد"
        );

        return;
    }

    const amount =
        Number(
            amountEl.value
        );

    if (
        !Number.isInteger(amount) ||
        amount <= 0
    ) {

        showToast(
            "تعداد نامعتبر است"
        );

        return;
    }

    try {

        const data =
            await api(
                "/sell",
                {
                    method: "POST",

                    body: JSON.stringify({
                        user_id: userId,
                        amount: amount
                    })
                }
            );

        showToast(
            `✅ فروش انجام شد — ${data.total.toLocaleString()} سکه`
        );

        await loadWallet();

        await loadPortfolio();

    }

    catch (error) {

        showToast(
            "❌ " + error.message
        );
    }
}


// =====================================
// PORTFOLIO
// =====================================

async function loadPortfolio() {

    if (!userId) {
        return;
    }

    try {

        const data =
            await api(
                "/portfolio/" + userId
            );

        sharesEl.textContent =
            data.shares;

        assetValueEl.textContent =
            Number(
                data.value
            ).toLocaleString();

    }

    catch (error) {

        console.error(error);
    }
}


// =====================================
// ADMIN PRICE
// =====================================

async function setPrice() {

    const price =
        Number(
            document.getElementById(
                "adminPrice"
            ).value
        );

    if (
        !Number.isInteger(price) ||
        price < 1
    ) {

        showToast(
            "قیمت نامعتبر است"
        );

        return;
    }

    try {

        await api(
            "/admin/price",
            {
                method: "POST",

                body: JSON.stringify({
                    user_id: userId,
                    price: price
                })
            }
        );

        showToast(
            "✅ قیمت تغییر کرد"
        );

        await loadMarket();

    }

    catch (error) {

        showToast(
            "❌ " + error.message
        );
    }
}


// =====================================
// RANDOM SETTINGS
// =====================================

async function setRandomSettings() {

    const minChange =
        Number(
            document.getElementById(
                "minChange"
            ).value
        );

    const maxChange =
        Number(
            document.getElementById(
                "maxChange"
            ).value
        );

    try {

        await api(
            "/admin/random",
            {
                method: "POST",

                body: JSON.stringify({
                    user_id: userId,
                    enabled: true,
                    min_change: minChange,
                    max_change: maxChange
                })
            }
        );

        showToast(
            "✅ تنظیمات ذخیره شد"
        );

    }

    catch (error) {

        showToast(
            "❌ " + error.message
        );
    }
}


// =====================================
// RANDOM UPDATE
// =====================================

async function randomUpdate() {

    try {

        const data =
            await api(
                "/market/random-update",
                {
                    method: "POST"
                }
            );

        showToast(
            `🎲 قیمت جدید: ${data.new_price}`
        );

        await loadMarket();

    }

    catch (error) {

        showToast(
            "❌ " + error.message
        );
    }
}


// =====================================
// CHART
// =====================================

function drawChart() {

    const rect =
        canvas.getBoundingClientRect();

    canvas.width =
        rect.width * devicePixelRatio;

    canvas.height =
        rect.height * devicePixelRatio;

    ctx.setTransform(
        devicePixelRatio,
        0,
        0,
        devicePixelRatio,
        0,
        0
    );

    const width =
        rect.width;

    const height =
        rect.height;

    ctx.clearRect(
        0,
        0,
        width,
        height
    );

    if (history.length < 2) {
        return;
    }

    const min =
        Math.min(...history);

    const max =
        Math.max(...history);

    const range =
        max - min || 1;

    ctx.beginPath();

    history.forEach(
        (value, index) => {

            const x =
                (index /
                    (history.length - 1))
                *
                width;

            const y =
                height -
                (
                    (value - min)
                    /
                    range
                )
                *
                (
                    height - 20
                )
                -
                10;

            if (index === 0) {
                ctx.moveTo(x, y);
            } else {
                ctx.lineTo(x, y);
            }
        }
    );

    ctx.strokeStyle =
        "#ffffff";

    ctx.lineWidth =
        3;

    ctx.stroke();
}


// =====================================
// TOAST
// =====================================

function showToast(message) {

    const toast =
        document.getElementById(
            "toast"
        );

    toast.textContent =
        message;

    toast.classList.add(
        "show"
    );

    setTimeout(
        () => {
            toast.classList.remove(
                "show"
            );
        },
        2500
    );
}


// =====================================
// START
// =====================================

async function start() {

    await loadMarket();

    await loadWallet();

    await loadPortfolio();

    // هر 5 ثانیه قیمت را بخوان
    setInterval(
        loadMarket,
        5000
    );

    setInterval(
        loadWallet,
        5000
    );
}


start();