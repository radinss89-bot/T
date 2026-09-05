// =========================================================
// TELEGRAM
// =========================================================

const tg = window.Telegram.WebApp;

tg.ready();
tg.expand();


// =========================================================
// API
// =========================================================

// اینجا آدرس Web Service رباتت را بگذار.
// مثال:
// https://my-bot.onrender.com

const API_URL = "https://YOUR-BOT-WEB-SERVICE.onrender.com";


// =========================================================
// USER
// =========================================================

const userId =
    tg.initDataUnsafe?.user?.id || null;


// =========================================================
// ELEMENTS
// =========================================================

const walletElement =
    document.getElementById("wallet");

const priceElement =
    document.getElementById("price");

const marketNameElement =
    document.getElementById("marketName");

const marketChangeElement =
    document.getElementById("marketChange");

const holdingElement =
    document.getElementById("holding");

const holdingValueElement =
    document.getElementById("holdingValue");

const amountElement =
    document.getElementById("amount");

const messageElement =
    document.getElementById("message");

const adminPanel =
    document.getElementById("adminPanel");

const adminPrice =
    document.getElementById("adminPrice");

const minChange =
    document.getElementById("minChange");

const maxChange =
    document.getElementById("maxChange");

const autoChange =
    document.getElementById("autoChange");


// =========================================================
// DATA
// =========================================================

let currentPrice = 0;

let previousPrice = null;

let chartPrices = [];


// =========================================================
// FORMAT
// =========================================================

function formatNumber(value) {

    return Number(value || 0)
        .toLocaleString("fa-IR", {
            maximumFractionDigits: 2
        });
}


// =========================================================
// MESSAGE
// =========================================================

function showMessage(text) {

    messageElement.textContent = text;

    setTimeout(() => {

        messageElement.textContent = "";

    }, 3000);
}


// =========================================================
// API HELPER
// =========================================================

async function api(path, options = {}) {

    const response = await fetch(
        `${API_URL}${path}`,
        {
            ...options,

            headers: {
                "Content-Type": "application/json",
                ...(options.headers || {})
            }
        }
    );

    const data =
        await response.json();

    if (!response.ok || data.ok === false) {

        throw new Error(
            data.error || "خطا در ارتباط با سرور"
        );
    }

    return data;
}


// =========================================================
// WALLET
// =========================================================

async function loadWallet() {

    if (!userId) {

        walletElement.textContent =
            "نامشخص";

        return;
    }

    try {

        const data =
            await api(`/wallet/${userId}`);

        walletElement.textContent =
            formatNumber(data.coins);

    } catch (error) {

        console.error(error);

        walletElement.textContent =
            "خطا";
    }
}


// =========================================================
// MARKET
// =========================================================

async function loadMarket() {

    try {

        const data =
            await api("/market");

        const market =
            data.market;

        currentPrice =
            Number(market.price);

        marketNameElement.textContent =
            market.name;

        priceElement.textContent =
            formatNumber(currentPrice);

        adminPrice.value =
            currentPrice;

        minChange.value =
            Number(market.min_change * 100)
                .toFixed(2);

        maxChange.value =
            Number(market.max_change * 100)
                .toFixed(2);

        autoChange.checked =
            market.auto_change;

        updateChange();

        addChartPrice(currentPrice);

    } catch (error) {

        console.error(error);

        priceElement.textContent =
            "خطا";
    }
}


// =========================================================
// PRICE CHANGE
// =========================================================

function updateChange() {

    if (
        previousPrice === null ||
        previousPrice === 0
    ) {

        marketChangeElement.textContent =
            "--";

        return;
    }

    const change =
        ((currentPrice - previousPrice)
            / previousPrice) * 100;

    const sign =
        change >= 0 ? "+" : "";

    marketChangeElement.textContent =
        `${sign}${change.toFixed(2)}%`;

    previousPrice =
        currentPrice;
}


// =========================================================
// CHART
// =========================================================

function addChartPrice(price) {

    chartPrices.push(price);

    if (chartPrices.length > 40) {

        chartPrices.shift();
    }

    drawChart();
}


function drawChart() {

    const canvas =
        document.getElementById("chart");

    const ctx =
        canvas.getContext("2d");

    const width =
        canvas.width;

    const height =
        canvas.height;

    ctx.clearRect(
        0,
        0,
        width,
        height
    );

    if (chartPrices.length < 2) {
        return;
    }

    let min =
        Math.min(...chartPrices);

    let max =
        Math.max(...chartPrices);

    if (min === max) {

        min -= 1;
        max += 1;
    }

    const padding = 20;

    ctx.beginPath();

    chartPrices.forEach(
        (price, index) => {

            const x =
                padding +
                (
                    index /
                    (chartPrices.length - 1)
                ) *
                (width - padding * 2);

            const y =
                height -
                padding -
                (
                    (price - min) /
                    (max - min)
                ) *
                (height - padding * 2);

            if (index === 0) {

                ctx.moveTo(x, y);

            } else {

                ctx.lineTo(x, y);
            }
        }
    );

    ctx.lineWidth = 4;

    ctx.strokeStyle = "#ffffff";

    ctx.lineCap = "round";

    ctx.lineJoin = "round";

    ctx.stroke();
}


// =========================================================
// PORTFOLIO
// =========================================================

async function loadPortfolio() {

    if (!userId) {

        holdingElement.textContent =
            "نامشخص";

        holdingValueElement.textContent =
            "نامشخص";

        return;
    }

    try {

        const data =
            await api(
                `/portfolio/${userId}`
            );

        holdingElement.textContent =
            formatNumber(data.amount);

        holdingValueElement.textContent =
            formatNumber(data.value);

    } catch (error) {

        console.error(error);

        holdingElement.textContent =
            "خطا";

        holdingValueElement.textContent =
            "خطا";
    }
}


// =========================================================
// BUY
// =========================================================

async function buy() {

    if (!userId) {

        showMessage(
            "کاربر تلگرام شناسایی نشد"
        );

        return;
    }

    const amount =
        Number(amountElement.value);

    if (
        !amount ||
        amount <= 0
    ) {

        showMessage(
            "مقدار نامعتبر است"
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

        showMessage(
            `خرید انجام شد؛ ${formatNumber(data.cost)} کوین کم شد`
        );

        await loadWallet();
        await loadPortfolio();

    } catch (error) {

        console.error(error);

        showMessage(
            error.message
        );
    }
}


// =========================================================
// SELL
// =========================================================

async function sell() {

    if (!userId) {

        showMessage(
            "کاربر تلگرام شناسایی نشد"
        );

        return;
    }

    const amount =
        Number(amountElement.value);

    if (
        !amount ||
        amount <= 0
    ) {

        showMessage(
            "مقدار نامعتبر است"
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

        showMessage(
            `فروش انجام شد؛ ${formatNumber(data.revenue)} کوین گرفتی`
        );

        await loadWallet();
        await loadPortfolio();

    } catch (error) {

        console.error(error);

        showMessage(
            error.message
        );
    }
}


// =========================================================
// ADMIN PRICE
// =========================================================

async function setAdminPrice() {

    if (!userId) {
        return;
    }

    const price =
        Number(adminPrice.value);

    if (
        !price ||
        price <= 0
    ) {

        showMessage(
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

        showMessage(
            "قیمت تغییر کرد"
        );

        previousPrice =
            currentPrice;

        await loadMarket();

    } catch (error) {

        console.error(error);

        showMessage(
            error.message
        );
    }
}


// =========================================================
// ADMIN SETTINGS
// =========================================================

async function saveAdminSettings() {

    if (!userId) {
        return;
    }

    const min =
        Number(minChange.value);

    const max =
        Number(maxChange.value);

    try {

        await api(
            "/admin/settings",
            {
                method: "POST",

                body: JSON.stringify({
                    user_id: userId,

                    min_change: min,

                    max_change: max,

                    auto_change:
                        autoChange.checked
                })
            }
        );

        showMessage(
            "تنظیمات ذخیره شد"
        );

    } catch (error) {

        console.error(error);

        showMessage(
            error.message
        );
    }
}


// =========================================================
// ADMIN CHECK
// =========================================================

function checkAdmin() {

    // همان ADMIN_ID داخل bot.py

    const ADMIN_ID =
        6235380364;

    if (
        userId &&
        Number(userId) === ADMIN_ID
    ) {

        adminPanel.style.display =
            "block";
    }
}


// =========================================================
// BUTTONS
// =========================================================

document
    .getElementById("buyBtn")
    .addEventListener(
        "click",
        buy
    );


document
    .getElementById("sellBtn")
    .addEventListener(
        "click",
        sell
    );


document
    .getElementById("setPriceBtn")
    .addEventListener(
        "click",
        setAdminPrice
    );


document
    .getElementById("saveSettingsBtn")
    .addEventListener(
        "click",
        saveAdminSettings
    );


// =========================================================
// AUTO REFRESH
// =========================================================

async function refresh() {

    try {

        const oldPrice =
            currentPrice;

        await loadMarket();

        if (
            oldPrice &&
            currentPrice !== oldPrice
        ) {

            addChartPrice(
                currentPrice
            );
        }

        await loadWallet();

        await loadPortfolio();

    } catch (error) {

        console.error(error);
    }
}


// =========================================================
// START
// =========================================================

async function start() {

    checkAdmin();

    await loadWallet();

    await loadMarket();

    await loadPortfolio();

    setInterval(
        refresh,
        10000
    );
}


start();